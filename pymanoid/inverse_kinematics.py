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

from numpy import dot, eye, hstack, maximum, minimum, ones, vstack, zeros
from qp import solve_qp
from threading import Lock
from warnings import warn


class DiffIKSolver(object):

    def __init__(self, q_max, q_min, qd_lim, K_doflim, gains=None,
                 weights=None):
        n = len(q_max)
        self.I = eye(n)
        self.K_doflim = K_doflim
        self.default_gains = {}
        self.default_weights = {}
        self.errors = {}
        self.gains = {}
        self.jacobians = {}
        self.q_max = q_max
        self.q_min = q_min
        self.qd_max = +qd_lim * ones(n)
        self.qd_min = -qd_lim * ones(n)
        self.tasks = {}
        self.tasks_lock = Lock()
        self.weights = {}
        if gains is not None:
            self.default_gains.update(gains)
        if weights is not None:
            self.default_weights.update(weights)

    def add_task(self, name, error, jacobian, gain=None, weight=None,
                 task_type=None, unit_gain=False):
        """
        Add a new task in the IK. A task is perfectly achieved when:

            jacobian(q) * qd == -gain * error(q, qd, dt) / dt     (1)

        To tend toward this, each task adds a term

            weight * |jacobian(q) * qd + gain * error(q, qd, dt) / dt|^2

        to the cost function of the optimization problem solved at each time
        step.

        INPUT:

        ``name`` -- task name, used as identifier for e.g. removal
        ``error`` -- error function of the task (depends on q, qd and dt)
        ``jacobian`` -- jacobian function of the task (depends on q only)
        ``gain`` -- task gain
        ``weight`` -- task weight
        ``task_type`` -- for some tasks such as contact, ``name`` corresponds to
            a robot link name; ``task_type`` is then used to fetch default gain
            and weight values
        ``unit_gain`` -- some tasks have a different formulation where the gain
            is one and there is no division by dt in Equation (1); set
            ``unit_gain=1`` for this behavior

        .. NOTE::

            This function is not made to be called frequently.

        """
        if name in self.tasks:
            raise Exception("Task '%s' already present in IK" % name)
        with self.tasks_lock:
            self.tasks[name] = True
            self.errors[name] = error
            self.jacobians[name] = jacobian
            if unit_gain:
                if name in self.gains:
                    del self.gains[name]
            elif gain is not None:
                self.gains[name] = gain
            elif name in self.default_gains:
                self.gains[name] = self.default_gains[name]
            elif task_type in self.default_gains:
                self.gains[name] = self.default_gains[task_type]
            else:
                msg = "No gain provided for task '%s'" % name
                if task_type is not None:
                    msg += " (task_type='%s')" % task_type
                raise Exception(msg)
            if weight is not None:
                self.weights[name] = weight
            elif name in self.default_weights:
                self.weights[name] = self.default_weights[name]
            elif task_type in self.default_weights:
                self.weights[name] = self.default_weights[task_type]
            else:
                msg = "No weight provided for task '%s'" % name
                if task_type is not None:
                    msg += " (task_type='%s')" % task_type
                raise Exception(msg)
            assert unit_gain or self.gains[name] < 1. + 1e-10, \
                "Task gains should be between 0. and 1 (%f)." % self.gains[name]
            assert self.weights[name] > 0., \
                "Task weights should be positive"

    def remove_task(self, name):
        with self.tasks_lock:
            if name not in self.tasks:
                warn("no task '%s' to remove" % name)
                return
            del self.tasks[name]

    def compute_cost(self, q, qd, dt):
        def sq(e):
            return dot(e, e)

        def cost(task):
            return self.weights[task] * sq(self.errors[task](q, qd, dt))

        return sum(cost(task) for task in self.tasks)

    def compute_velocity(self, q, qd, dt):
        P = zeros(self.I.shape)
        r = zeros(self.q_max.shape)
        with self.tasks_lock:
            for task in self.tasks:
                J = self.jacobians[task](q)
                if task in self.gains:
                    e = self.gains[task] * (self.errors[task](q, qd, dt) / dt)
                else:  # for tasks added with unit_gain=True
                    e = self.errors[task](q, qd, dt)
                P += self.weights[task] * dot(J.T, J)
                r += self.weights[task] * dot(-e.T, J)
        qd_max = minimum(self.qd_max, self.K_doflim * (self.q_max - q))
        qd_min = maximum(self.qd_min, self.K_doflim * (self.q_min - q))
        G = vstack([+self.I, -self.I])
        h = hstack([qd_max, -qd_min])
        return solve_qp(P, r, G, h)
