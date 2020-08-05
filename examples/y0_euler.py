__copyright__ = """
Copyright (C) 2020 University of Illinois Board of Trustees
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""
import logging
# import numpy as np
import pyopencl as cl
import numpy.linalg as la  # noqa
import pyopencl.array as cla  # noqa
from functools import partial
from mpi4py import MPI

from meshmode.array_context import PyOpenCLArrayContext
from meshmode.dof_array import thaw
from meshmode.mesh import BTAG_ALL, BTAG_NONE  # noqa
from grudge.eager import EagerDGDiscretization
from grudge.shortcuts import make_visualizer


from mirgecom.euler import inviscid_operator
from mirgecom.simutil import (
    inviscid_sim_timestep,
    exact_sim_checkpoint
)
from mirgecom.io import make_init_message

from mirgecom.integrators import rk4_step
from mirgecom.steppers import advance_state
from mirgecom.boundary import PrescribedBoundary
from mirgecom.initializers import Uniform
from mirgecom.eos import IdealSingleGas


def import_pseudo_y0_mesh():

    from meshmode.mesh.io import generate_gmsh, ScriptWithFilesSource
    mesh = generate_gmsh(
        ScriptWithFilesSource("""
        Merge "pseudoY0.brep";
        Mesh.CharacteristicLengthMin = 1;
        Mesh.CharacteristicLengthMax = 10;
        Mesh.ElementOrder = 2;
        Mesh.CharacteristicLengthExtendFromBoundary = 0;

        // Inside and end surfaces of nozzle/scramjet
        Field[1] = Distance;
        Field[1].NNodesByEdge = 100;
        Field[1].FacesList = {5,7,8,9,10};
        Field[2] = Threshold;
        Field[2].IField = 1;
        Field[2].LcMin = 1;
        Field[2].LcMax = 10;
        Field[2].DistMin = 0;
        Field[2].DistMax = 20;

        // Edges separating surfaces with boundary layer
        // refinement from those without
        // (Seems to give a smoother transition)
        Field[3] = Distance;
        Field[3].NNodesByEdge = 100;
        Field[3].EdgesList = {5,10,14,16};
        Field[4] = Threshold;
        Field[4].IField = 3;
        Field[4].LcMin = 1;
        Field[4].LcMax = 10;
        Field[4].DistMin = 0;
        Field[4].DistMax = 20;

        // Min of the two sections above
        Field[5] = Min;
        Field[5].FieldsList = {2,4};

        Background Field = 5;
        """, ["pseudoY0.brep"]), 3, target_unit='MM')
    return mesh


def main(ctx_factory=cl.create_some_context):

    cl_ctx = ctx_factory()
    queue = cl.CommandQueue(cl_ctx)
    actx = PyOpenCLArrayContext(queue)

    logger = logging.getLogger(__name__)

    dim = 3
    order = 1
    exittol = .09
    t_final = 0.01
    current_cfl = 1.0
    #    vel = np.zeros(shape=(dim,))
    #    orig = np.zeros(shape=(dim,))
    #    vel[:dim] = 1.0
    current_dt = .001
    current_t = 0
    eos = IdealSingleGas()
    initializer = Uniform(numdim=dim)
    casename = 'pseudoY0'
    boundaries = {BTAG_ALL: PrescribedBoundary(initializer)}
    constant_cfl = False
    nstatus = 1
    nviz = 1
    rank = 0
    checkpoint_t = current_t
    current_step = 0
    timestepper = rk4_step

    comm = MPI.COMM_WORLD
    nproc = comm.Get_size()
    rank = comm.Get_rank()
    num_parts = nproc

    from meshmode.distributed import (
        MPIMeshDistributor,
        get_partition_by_pymetis,
    )

    global_nelements = 0
    local_nelements = 0

    if nproc > 1:
        mesh_dist = MPIMeshDistributor(comm)
        if mesh_dist.is_mananger_rank():

            mesh = import_pseudo_y0_mesh()
            global_nelements = mesh.nelements
            logging.info(f"Total {dim}d elements: {global_nelements}")

            part_per_element = get_partition_by_pymetis(mesh, num_parts)

            local_mesh = mesh_dist.send_mesh_parts(mesh, part_per_element, num_parts)
            del mesh

        else:
            local_mesh = mesh_dist.receive_mesh_part()
    else:
        local_mesh = import_pseudo_y0_mesh()
        global_nelements = local_mesh.nelements

    local_nelements = local_mesh.nelements

    discr = EagerDGDiscretization(
        actx, local_mesh, order=order, mpi_communicator=comm
    )
    nodes = thaw(actx, discr.nodes())
    current_state = initializer(0, nodes)

    visualizer = make_visualizer(discr, discr.order + 3
                                 if discr.dim == 2 else discr.order)

    initname = initializer.__class__.__name__
    eosname = eos.__class__.__name__
    init_message = make_init_message(dim=dim, order=order,
                                     nelements=local_nelements,
                                     global_nelements=global_nelements,
                                     dt=current_dt, t_final=t_final, nstatus=nstatus,
                                     nviz=nviz, cfl=current_cfl,
                                     constant_cfl=constant_cfl, initname=initname,
                                     eosname=eosname, casename=casename)
    if rank == 0:
        logger.info(init_message)

    get_timestep = partial(inviscid_sim_timestep, discr=discr, t=current_t,
                           dt=current_dt, cfl=current_cfl, eos=eos,
                           t_final=t_final, constant_cfl=constant_cfl)

    def my_rhs(t, state):
        return inviscid_operator(discr, q=state, t=t,
                                 boundaries=boundaries, eos=eos)

    def my_checkpoint(step, t, dt, state):
        return exact_sim_checkpoint(discr, initializer, visualizer, eos, logger,
                            q=state, vizname=casename, step=step, t=t, dt=dt,
                            nstatus=nstatus, nviz=nviz, exittol=exittol,
                            constant_cfl=constant_cfl, comm=comm)

    (current_step, current_t, current_state) = \
        advance_state(rhs=my_rhs, timestepper=timestepper, checkpoint=my_checkpoint,
                    get_timestep=get_timestep, state=current_state,
                    t=current_t, t_final=t_final)

    if rank == 0:
        logger.info("Checkpointing final state ...")
        my_checkpoint(current_step, t=current_t,
                      dt=(current_t - checkpoint_t),
                      state=current_state)

    if current_t - t_final < 0:
        raise ValueError("Simulation exited abnormally")


if __name__ == "__main__":
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    main()

# vim: foldmethod=marker