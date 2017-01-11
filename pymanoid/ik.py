#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016 Stephane Caron <stephane.caron@normalesup.org>
#
# This file is part of pymanoid <https://github.com/stephane-caron/pymanoid>.
#
# pymanoid is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# pymanoid is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# pymanoid. If not, see <http://www.gnu.org/licenses/>.

from numpy import dot, eye, hstack, maximum, minimum, vstack, zeros, bmat, ones
from optim import solve_qp
from threading import Lock
from warnings import warn


class IKError(Exception):

    pass


class VelocitySolver(object):

    """
    Compute velocities bringing the system closer to fulfilling a set of tasks.
    """

    def __init__(self, robot, active_dofs, doflim_gain):
        """
        Initialize the solver.

        INPUT:

        - ``robot`` -- upper DOF limit
        - ``active_dofs`` -- list of DOFs used by the IK
        - ``doflim_gain`` -- gain between 0 and 1 used for DOF limits

        The ``doflim_gain`` is described in [Kanoun2012]_. In this
        implementation, it should be between 0. and 1. [Caron2016]_. One
        unsatisfactory aspect of this solution is that it artificially slows
        down the robot when approaching DOF limits. For instance, it may slow
        down a foot motion when approaching the knee singularity, despite the
        robot being able to move faster with a fully extended knee.

        REFERENCES:

        .. [Caron2016] <https://scaron.info/teaching/inverse-kinematics.html>
        .. [Kanoun2012] <http://www.roboticsproceedings.org/rss07/p21.pdf>
        """
        assert 0. <= doflim_gain <= 1.
        nb_active_dofs = len(active_dofs)
        self.active_dofs = active_dofs
        self.doflim_gain = doflim_gain
        self.nb_active_dofs = len(active_dofs)
        self.q_max = robot.q_max[active_dofs]
        self.q_min = robot.q_min[active_dofs]
        self.qd = zeros(robot.nb_dofs)
        self.qd_max = +1. * ones(nb_active_dofs)
        self.qd_min = -1. * ones(nb_active_dofs)
        self.robot = robot
        self.tasks = {}
        self.tasks_lock = Lock()

    def add_task(self, task):
        """
        Add a new task in the IK.

        INPUT:

        - ``task`` -- Task object

        .. NOTE::

            This function is not made to be called frequently.
        """
        task.check()
        if task.name in self.tasks:
            raise Exception("Task '%s' already present in IK" % task.name)
        with self.tasks_lock:
            self.tasks[task.name] = task

    def add_tasks(self, tasks):
        for task in tasks:
            self.add_task(task)

    def get_task(self, name):
        """
        Get an active task from its name.

        INPUT:

        - ``name`` -- task name

        OUTPUT:

        The corresponding task object.
        """
        with self.tasks_lock:
            if name not in self.tasks:
                warn("no task with name '%s'" % name)
                return
            return self.tasks[name]

    def remove_task(self, name):
        """
        Remove a task from the IK.

        INPUT:

        - ``name`` -- task name
        """
        with self.tasks_lock:
            if name not in self.tasks:
                warn("no task '%s' to remove" % name)
                return
            del self.tasks[name]

    def compute_cost(self, dt):
        return sum(task.cost(dt) for task in self.tasks.itervalues())

    def __compute_qp_common(self, dt):
        n = self.nb_active_dofs
        q = self.robot.q[self.active_dofs]
        qp_P = zeros((n, n))
        qp_q = zeros(n)
        with self.tasks_lock:
            for task in self.tasks.itervalues():
                J = task.jacobian()[:, self.active_dofs]
                r = task.residual(dt)
                qp_P += task.weight * dot(J.T, J)
                qp_q += task.weight * dot(-r.T, J)
        qd_max_doflim = self.doflim_gain * (self.q_max - q) / dt
        qd_min_doflim = self.doflim_gain * (self.q_min - q) / dt
        qd_max = minimum(self.qd_max, qd_max_doflim)
        qd_min = maximum(self.qd_min, qd_min_doflim)
        return (qp_P, qp_q, qd_max, qd_min)

    def __solve_qp(self, qp_P, qp_q, qp_G, qp_h):
        try:
            qd_active = solve_qp(qp_P, qp_q, qp_G, qp_h)
            self.qd[self.active_dofs] = qd_active
        except ValueError as e:
            if "matrix G is not positive definite" in e:
                msg = "rank deficiency. Did you add a regularization task?"
                raise IKError(msg)
            raise
        return self.qd

    def compute_velocity_fast(self, dt):
        """
        Compute a new velocity satisfying all tasks at best, while staying
        within joint-velocity limits.

        INPUT:

        - ``dt`` -- time step in [s]

        OUTPUT:

        Active joint velocity vector.

        ALGORITHM:

        Minimizes squared residuals as in the weighted cost function, which
        corresponds to the Gauss-Newton algorithm. Indeed, expanding the square
        expression in cost(task, qd) yields

            minimize    qd * (J.T * J) * qd - 2 * (residual / dt) * J * qd

        Differentiating with respect to ``qd`` shows that the minimum is
        attained for (J.T * J) * qd == (residual / dt), where we recognize the
        Gauss-Newton update rule.

        .. NOTE::

            This method is reasonably fast but may become unstable when some
            tasks are widely infeasible and the optimum saturates joint limits.
            In such situations, you can use ``compute_velocity_safe()`` instead.
        """
        n = self.nb_active_dofs
        qp_P, qp_q, qd_max, qd_min = self.__compute_qp_common(dt)
        qp_G = vstack([+eye(n), -eye(n)])
        qp_h = hstack([qd_max, -qd_min])
        return self.__solve_qp(qp_P, qp_q, qp_G, qp_h)

    def compute_velocity_safe(self, dt, margin_reg=1e-5, margin_lin=1e-3):
        """
        Compute a new velocity satisfying all tasks at best, while staying
        within joint-velocity limits.

        INPUT:

        - ``dt`` -- time step in [s]
        - ``margin_reg`` -- regularization term on margin variables
        - ``margin_lin`` -- linear penalty term on margin variables

        OUTPUT:

        Active joint velocity vector.

        ALGORITHM:

        Variation on the QP from ``compute_velocity_fast()`` reported in Equ.
        (10) of [Nozawa2016]_. DOF limits are better taken care of by margin
        variables, but the variable count doubles and the QP takes roughly 50%
        more time to solve.

        REFERENCES:

        .. [Nozawa2016] Nozawa, Shunichi, et al. "Three-dimensional humanoid
           motion planning using COM feasible region and its application to
           ladder climbing tasks." Humanoid Robots (Humanoids), 2016 IEEE-RAS
           16th International Conference on. IEEE, 2016.
        """
        n = self.nb_active_dofs
        E, Z = eye(n), zeros((n, n))
        qp_P0, qp_q0, qd_max, qd_min = self.__compute_qp_common(dt)
        qp_P = bmat([[qp_P0, Z], [Z, margin_reg * E]])
        qp_q = hstack([qp_q0, -margin_lin * ones(n)])
        qp_G = bmat([[+E, +E / dt], [-E, +E / dt], [Z, -E]])
        qp_h = hstack([qd_max, -qd_min, zeros(n)])
        return self.__solve_qp(qp_P, qp_q, qp_G, qp_h)

    def compute_velocity(self, dt, method):
        """
        Compute a new velocity satisfying all tasks at best, while staying
        within joint-velocity limits.

        INPUT:

        - ``dt`` -- time step in [s]
        - ``method`` -- choice between 'fast' and 'safe'

        OUTPUT:

        Active joint velocity vector.
        """
        if method == 'fast':
            return self.compute_velocity_fast(dt)
        return self.compute_velocity_safe(dt)
