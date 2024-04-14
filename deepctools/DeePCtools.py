"""
Name: DeePCtools.py
Author: Xuewen Zhang
Date:at 13/04/2024
version: 1.0.0
Description: Toolbox to formulate the DeePC problem
"""
import time
import warnings
import numpy as np
import casadi as cs
import casadi.tools as ctools

# packages from deepctools
from . import util

def timer(f):
    def wrapper(*args, **kwargs):
        start = time.time()
        ret = f(*args, **kwargs)
        print('Time elapsed: {}'.format(time.time() - start))
        return ret

    return wrapper


class deepctools():
    """
         ----------------------------------------------------------------------------------------------------------
                Standard DeePC design:                    |            Equivalent expression
         min J  =  || y - yref ||_Q^2 + || uloss ||_R^2   |   min J  =  || Uf*g - yref ||_Q^2 + || uloss ||_R^2
             s.t.   [Up]       [uini]                     |      s.t.   Up * g = uini
                    [Yp] * g = [yini]                     |             Yp * g = yini
                    [Uf]       [ u  ]                     |             ulb <= u <= uub
                    [Yf]       [ y  ]                     |             ylb <= y <= yub
                    ulb <= u <= uub                       |
                    ylb <= y <= yub                       |  uloss = (u)   or    (u - uref)    or    (du)
         -------------------------------------------------|----------------------------------------------------------
                  Robust DeePC design:                    |            Equivalent expression
         min J  =  || y - yref ||_Q^2 + || uloss ||_R^2   |   min J  =  || Uf*g - ys ||_Q^2 + || uloss ||_R^2
                     + lambda_y||sigma_y||_2^2            |             + lambda_y||Yp*g-yini||_2^2
                     + lambda_g||g||_2^2                  |             + lambda_g||g||_2^2
             s.t.   [Up]       [uini]     [   0   ]       |      s.t.   Up * g = uini
                    [Yp] * g = [yini]  +  [sigma_y]       |             ulb <= u <= uub
                    [Uf]       [ u  ]     [   0   ]       |             ylb <= y <= yub
                    [Yf]       [ y  ]     [   0   ]       |
                    ulb <= u <= uub                       |
                    ylb <= y <= yub                       |  uloss = (u)   or    (u - uref)    or    (du)
         ----------------------------------------------------------------------------------------------------------
                        Functions                |                            Usage
            hankel(x, L)                         |  construct Hankel matrix based on given data x and order L
            initialize_DeePCsolver(uloss, opts)  |  construct DeePC solver
            initialize_RDeePCsolver(uloss, opts) |  construct Robust DeePC solver
            solver_step(uini, yini, g0_guess)    |  solve the optimization problem one step
         ----------------------------------------------------------------------------------------------------------
    """

    def __init__(self, u_dim, y_dim, T, Tini, Np, ys, ud, yd, Q, R, us=None, lambda_g=None, lambda_y=None,
                 ineqconidx=None, ineqconbd=None):
        """
            ------Initialize the system parameters and DeePC config------
                 u_dim: [int]             |  the dimension of control inputs
                 y_dim: [int]             |  the dimension of controlled outputs
                     T: [int]             |  the length of offline collected data
                  Tini: [int]             |  the initialization length of the online loop
                    Np: [int]             |  the length of predicted future trajectories
                    us: [array]           |  set-point of u;  size (1, u_dim)
                    ys: [array]           |  set-point of y;  size (1, y_dim)
                    ud: [array]           |  history data collected offline to construct Hankel matrix; size (T, u_dim)
                    yd: [array]           |  history data collected offline to construct Hankel matrix; size (T, u_dim)
                     Q: [array]           |  the weighting matrix of controlled outputs y
                     R: [array]           |  the weighting matrix of control inputs u
              lambda_y: [array]           |  the weighting matrix of mismatch of controlled output y
              lambda_g: [array]           |  the weighting matrix of norm of operator g
            ineqconidx: [dict|[str,list]] |  specify the wanted constraints for u and y, if None, no constraints
                                          |      e.g., only have constraints on u2, u3, {'u': [1,2]}; 'y' as well
             ineqconbd: [dict|[str,list]] |  specify the bounds for u and y, should be consistent with "ineqconidx"
                                          |      e.g., bound on u2, u3, {'lbu': [1,0], 'ubu': [10,5]}; lby, uby as well
        """

        self.u_dim = u_dim
        self.y_dim = y_dim
        self.T = T
        self.Tini = Tini
        self.Np = Np
        self.g_dim = T - Tini - Np + 1
        self.Q = Q
        self.R = R
        self.lambda_g = lambda_g
        self.lambda_y = lambda_y
        self.us = us
        self.ys = ys
        self.yref = np.tile(ys, (Np, 1)).reshape(-1, 1)
        self.uref = None
        if us.any():
            self.uref = np.tile(us, (Np, 1)).reshape(-1, 1)

        self.Hud = util.hankel(ud, Tini + Np)
        self.Hyd = util.hankel(yd, Tini + Np)
        self.Up = self.Hud[:self.u_dim * self.Tini, :]
        self.Uf = self.Hud[self.u_dim * self.Tini:, :]
        self.Yp = self.Hyd[:self.y_dim * self.Tini, :]
        self.Yf = self.Hyd[self.y_dim * self.Tini:, :]

        # check variable size
        self._checkvar()

        # init inequality constrains
        self.Hc, self.lbc_ineq, self.ubc_ineq = self._init_ineq_cons(ineqconidx, ineqconbd)

        # init the casadi variables
        self._init_variables()

        self.solver = None
        self.lbc = None
        self.ubc = None

    def _checkvar(self):
        """
            ------Check the variables if consist with DeePC config------
                  Variables    |             Shape
            -------------------|----------------------------------------
            ud, yd             |  (T, dim)
            Hud, Hyd           |  (dim*L, T-L+1),  L = Tini + Np
            Up, Yp             |  (dim*Tini, T-L+1)
            Uf, Yf             |  (dim*Np, T-L+1)
            uref, yref         |  (dim*Np, 1)
            Q, R               |  (dim*Np, dim*Np)
            lambda_g           |  (T-L+1, T-L+1)
            lambda_y           |  (dim*Tini, dim*Tini)
            ------------------------------------------------------------
            Persistently Excitation condition:
                g_dim >= u_dim * (Tini + Np): to satisfy
                and the Hankel matrix of ud should be full row rank
                which is u_dim * (Tini + Np)
            ------------------------------------------------------------
        """
        self._checkshape(self.Up, tuple([self.u_dim * self.Tini, self.g_dim]))
        self._checkshape(self.Yp, tuple([self.y_dim * self.Tini, self.g_dim]))
        self._checkshape(self.Uf, tuple([self.u_dim * self.Np, self.g_dim]))
        self._checkshape(self.Yf, tuple([self.y_dim * self.Np, self.g_dim]))
        self._checkshape(self.uref, tuple([self.u_dim * self.Np, 1]))
        self._checkshape(self.yref, tuple([self.y_dim * self.Np, 1]))
        self._checkshape(self.Q, tuple([self.y_dim * self.Np, self.y_dim * self.Np]))
        self._checkshape(self.R, tuple([self.u_dim * self.Np, self.u_dim * self.Np]))
        self._checkshape(self.lambda_g, tuple([self.g_dim, self.g_dim]))
        self._checkshape(self.lambda_y, tuple([self.y_dim * self.Tini, self.y_dim * self.Tini]))

        # Check PE condition
        if self.g_dim < self.u_dim * (self.Tini + self.Np):
            warnings.warn("Persistently Excitation (PE) condition not satisfied! Should g_dim >= u_dim * (Tini + Np)")
        Hud_rank = np.linalg.matrix_rank(self.Hud)
        if Hud_rank != self.u_dim * (self.Tini + self.Np):
            warnings.warn(f"Persistently Excitation (PE) condition not satisfied! Should Hankel matrix of ud is full row rank: u_dim * (Tini + Np) = {self.u_dim * (self.Tini + self.Np)} != {Hud_rank}!")


    def _checkshape(self, x, x_shape):
        if x is not None:
            if x.shape != x_shape:
                raise ValueError(f'Inconsistent detected: {x.shape} != {x_shape}!')

    def _init_ineq_cons(self, ineqconidx=None, ineqconbd=None):
        """
            Obtain Hankel matrix that used for the inequality constrained variables
                           lbc <= Hc * g <= ubc
            return  Hc, lbc, ubc
        """
        if ineqconidx is None:
            print(">> DeePC design have no constraints on 'u' and 'y'.")
            Hc, lbc, ubc = [], [], []
        else:
            Hc_list = []
            lbc_list = []
            ubc_list = []
            for varname, idx in ineqconidx.items():
                if varname == 'u':
                    H_all = self.Uf.copy()
                    dim = self.u_dim
                    lb = ineqconbd['lbu']
                    ub = ineqconbd['ubu']
                elif varname == 'y':
                    H_all = self.Yf.copy()
                    dim = self.y_dim
                    lb = ineqconbd['lby']
                    ub = ineqconbd['uby']
                else:
                    raise ValueError("%s variable not exist, should be 'u' or/and 'y'!" % varname)

                idx_H = [v + i * dim for i in range(self.Np) for v in idx]
                Hc_list.append(H_all[idx_H, :])
                lbc_list.append(np.tile(lb, self.Np))
                ubc_list.append(np.tile(ub, self.Np))

            Hc = np.concatenate(Hc_list)
            lbc = np.concatenate(lbc_list).flatten()
            ubc = np.concatenate(ubc_list).flatten()
        return Hc, lbc.tolist(), ubc.tolist()

    def _init_variables(self):
        """
            Initialize variables of DeePC and RDeePC design
                   parameters: uini, yini   |   updated each iteration
            optimizing_target: g            |   decision variable
        """
        ## define casadi variables
        self.parameters = ctools.struct_symSX([
            (
                ctools.entry('uini', shape=tuple([self.u_dim * self.Tini, 1])),
                ctools.entry('yini', shape=tuple([self.y_dim * self.Tini, 1]))
            )
        ])

        self.optimizing_target = ctools.struct_symSX([
            (
                ctools.entry('g', shape=tuple([self.g_dim, 1]))
            )
        ])

    @timer
    def init_DeePCsolver(self, uloss='u', opts={}):
        """
                              Formulate NLP solver for: DeePC design
            Initialize CasADi nlp solver, !!! only need to formulate the nlp problem at the first time !!!
            treat the updated variables as parameters of the nlp problem
            At each time, update the initial guess of the decision variables and the required parameters
            ----------------------------------------------------------------------------------------------
            nlp_prob = {'f': obj, 'x': optimizing_target, 'p': parameters, 'g': cs.vertcat(*C)}
            self.solver = cs.nlpsol('solver', 'ipopt', nlp_prob, opts)
            sol = self.solver(x0=g_guess, p=parameters, lbg=self.lbc, ubg=self.ubc)
            uloss: the loss of u in objective function
                   "u"  :  ||u||_R^2
                   "uus":  ||u - us||_R^2
                   "du" :  || du ||_R^2
            opts: the config of the solver; max iteration, print level, etc.
                   e.g.:         opts = {
                                            'ipopt.max_iter': 100,  # 50
                                            'ipopt.tol': 1e-5,
                                            'ipopt.print_level': 1,
                                            'print_time': 0,
                                            # 'ipopt.acceptable_tol': 1e-8,
                                            # 'ipopt.acceptable_obj_change_tol': 1e-6,
                                        }
            -----------------------------------------------------------------------------------------------
            g_dim:
                if DeePC:
                    g_dim >= (u_dim + y_dim) * Tini
                if Robust DeePC:
                    g_dim >= u_dim * Tini
                to ensure have enough degree of freedom of nlp problem for g
                this is the equality constraints should be less than decision variables
            -----------------------------------------------------------------------------------------------
        """
        print('>> DeePC design formulating')
        if uloss not in ["u", "uus", "du"]:
            raise ValueError("uloss should be one of: 'u', 'uus', 'du'!")
        if self.g_dim <= (self.u_dim + self.y_dim) * self.Tini:
            raise ValueError(f'NLP do not have enough degrees of freedom | Should: g_dim >= (u_dim + y_dim) * Tini, but got: {self.g_dim} <= {(self.u_dim + self.y_dim) * self.Tini}!')

        # define parameters and decision variable
        uini, yini = self.parameters[...]
        g, = self.optimizing_target[...]  # data are stored in list [], notice that ',' cannot be missed

        ## J  =  || Uf * g - ys ||_Q^2 + || uloss ||_R^2
        ## s.t.   Up * g = uini
        ##        Yp * g = yini
        ##        ulb <= u <= uub

        ## objective function in QP form
        if uloss == 'u':
            ## QP problem
            H = self.Yf.T @ self.Q @ self.Yf + self.Uf.T @ self.R @ self.Uf
            f = - self.Yf.T @ self.Q @ self.yref  # - self.Uf.T @ self.R @ uref
            obj = 0.5 * cs.mtimes(cs.mtimes(g.T, H), g) + cs.mtimes(f.T, g)

        if uloss == 'uus':
            if self.uref is None:
                raise ValueError("Do not give value of 'us', but required in objective function 'u-us'!")
            ## QP problem
            H = self.Yf.T @ self.Q @ self.Yf + self.Uf.T @ self.R @ self.Uf
            f = - self.Yf.T @ self.Q @ self.yref - self.Uf.T @ self.R @ self.uref
            obj = 0.5 * cs.mtimes(cs.mtimes(g.T, H), g) + cs.mtimes(f.T, g)

        if uloss == 'du':
            ## Not a QP problem
            u_cur = cs.mtimes(self.Uf, g)
            u_prev = cs.vertcat(uini[-self.u_dim:], cs.mtimes(self.Uf, g)[:-self.u_dim])
            du = u_cur - u_prev

            y = cs.mtimes(self.Yf, g)
            y_loss = y - self.yref
            obj = cs.mtimes(cs.mtimes(y_loss.T, self.Q), y_loss) + cs.mtimes(cs.mtimes(du.T, self.R), du)

        #### constrains
        C = []
        lbc, ubc = [], []
        # equal constrains:  Up * g = uini, Yp * g = yini
        C += [cs.mtimes(self.Up, g) - uini]
        for i in range(uini.shape[0]):
            lbc += [0]
            ubc += [0]
        C += [cs.mtimes(self.Yp, g) - yini]
        for i in range(yini.shape[0]):
            lbc += [0]
            ubc += [0]

        # inequality constrains:    ulb <= Uf_u * g <= uub --> only original u
        C += [cs.mtimes(self.Hc, g)]
        lbc.extend(self.lbc_ineq)
        ubc.extend(self.ubc_ineq)

        # formulate the nlp prolbem
        nlp_prob = {'f': obj, 'x': self.optimizing_target, 'p': self.parameters, 'g': cs.vertcat(*C)}

        self.solver = cs.nlpsol('solver', 'ipopt', nlp_prob, opts)
        self.lbc = lbc
        self.ubc = ubc

    @timer
    def init_RDeePCsolver(self, uloss='u', opts={}):
        """
                              Formulate NLP solver for: Robust DeePC design
            Initialize CasADi nlp solver, !!! only need to formulate the nlp problem at the first time !!!
            treat the updated variables as parameters of the nlp problem
            At each time, update the initial guess of the decision variables and the required parameters
            ----------------------------------------------------------------------------------------------
            nlp_prob = {'f': obj, 'x': optimizing_target, 'p': parameters, 'g': cs.vertcat(*C)}
            self.solver = cs.nlpsol('solver', 'ipopt', nlp_prob, opts)
            sol = self.solver(x0=g_guess, p=parameters, lbg=self.lbc, ubg=self.ubc)
            uloss: the loss of u in objective function
                   "u"  :  ||u||_R^2
                   "uus":  ||u - us||_R^2
                   "du" :  || du ||_R^2
            opts: the config of the solver; max iteration, print level, etc.
                   e.g.:         opts = {
                                            'ipopt.max_iter': 100,  # 50
                                            'ipopt.tol': 1e-5,
                                            'ipopt.print_level': 1,
                                            'print_time': 0,
                                            # 'ipopt.acceptable_tol': 1e-8,
                                            # 'ipopt.acceptable_obj_change_tol': 1e-6,
                                        }
            ----------------------------------------------------------------------------------------------
            g_dim:
                if DeePC:
                    g_dim >= (u_dim + y_dim) * Tini
                if Robust DeePC:
                    g_dim >= u_dim * Tini
                to ensure have enough degree of freedom of nlp problem for g
                this is the equality constraints should be less than decision variables
            -----------------------------------------------------------------------------------------------
        """
        print('>> Robust DeePC design formulating')
        if uloss not in ["u", "uus", "du"]:
            raise ValueError("uloss should be one of: 'u', 'uus', 'du'!")
        if self.lambda_g is None or self.lambda_y is None:
            raise ValueError(
                "Do not give value of 'lambda_g' or 'lambda_y', but required in objective function for Robust DeePC!")
        if self.g_dim <= self.u_dim * self.Tini:
            raise ValueError(f'NLP do not have enough degrees of freedom | Should: g_dim >= u_dim * Tini, but got: {self.g_dim} <= {self.u_dim * self.Tini}!')


        # define parameters and decision variable
        uini, yini = self.parameters[...]
        g, = self.optimizing_target[...]  # data are stored in list [], notice that ',' cannot be missed

        ## J  =  || Uf * g - ys ||_Q^2 + || uloss ||_R^2 + lambda_y || Yp * g - yini||_2^2 + lambda_g || g ||_2^2
        ## s.t.   Up * g = uini
        ##        ulb <= u <= uub

        ## objective function
        if uloss == 'u':
            ## QP problem
            H = self.Yf.T @ self.Q @ self.Yf + self.Uf.T @ self.R @ self.Uf + self.Yp.T @ self.lambda_y @ self.Yp + self.lambda_g
            f = - self.Yp.T @ self.lambda_y @ yini - self.Yf.T @ self.Q @ self.yref  # - self.Uf.T @ self.R @ uref
            obj = 0.5 * cs.mtimes(cs.mtimes(g.T, H), g) + cs.mtimes(f.T, g)

        if uloss == 'uus':
            if self.uref is None:
                raise ValueError("Do not give value of 'us', but required in objective function 'u-us'!")
            ## QP problem
            H = self.Yf.T @ self.Q @ self.Yf + self.Uf.T @ self.R @ self.Uf + self.Yp.T @ self.lambda_y @ self.Yp + self.lambda_g
            f = - self.Yp.T @ self.lambda_y @ yini - self.Yf.T @ self.Q @ self.yref - self.Uf.T @ self.R @ self.uref
            obj = 0.5 * cs.mtimes(cs.mtimes(g.T, H), g) + cs.mtimes(f.T, g)

        if uloss == 'du':
            ## Not a QP problem
            u_cur = cs.mtimes(self.Uf, g)
            u_prev = cs.vertcat(uini[-self.u_dim:], cs.mtimes(self.Uf, g)[:-self.u_dim])
            du = u_cur - u_prev

            y = cs.mtimes(self.Yf, g)
            y_loss = y - self.yref

            sigma_y = cs.mtimes(self.Yp, g) - yini
            obj = cs.mtimes(cs.mtimes(y_loss.T, self.Q), y_loss) + cs.mtimes(cs.mtimes(du.T, self.R), du) + cs.mtimes(
                cs.mtimes(g.T, self.lambda_g), g) + cs.mtimes(cs.mtimes(sigma_y.T, self.lambda_y), sigma_y)

        #### constrains
        C = []
        lbc, ubc = [], []
        # equal constrains:  Up * g = uini, Yp * g = yini
        C += [cs.mtimes(self.Up, g) - uini]
        for i in range(uini.shape[0]):
            lbc += [0]
            ubc += [0]

        # inequality constrains:    ulb <= Uf_u * g <= uub --> only original u
        C += [cs.mtimes(self.Hc, g)]
        lbc.extend(self.lbc_ineq)
        ubc.extend(self.ubc_ineq)

        # formulate the nlp prolbem
        nlp_prob = {'f': obj, 'x': self.optimizing_target, 'p': self.parameters, 'g': cs.vertcat(*C)}

        self.solver = cs.nlpsol('solver', 'ipopt', nlp_prob, opts)
        self.lbc = lbc
        self.ubc = ubc

    def solver_step(self, uini, yini):
        """
            solver solve the nlp for one time
            uini, yini:  [array]   | (dim*Tini, 1)
              g0_guess:  [array]   | (T-L+1, 1)
            return:
                u_opt:  the optimized control input for the next Np steps
                 g_op:  the optimized operator g
                  t_s:  solving time
        """
        parameters = np.concatenate((uini, yini))
        g0_guess = np.linalg.pinv(np.concatenate((self.Up, self.Yp), axis=0)) @ parameters

        t_ = time.time()
        sol = self.solver(x0=g0_guess, p=parameters, lbg=self.lbc, ubg=self.ubc)
        t_s = time.time() - t_

        g_opt = sol['x'].full().ravel()
        u_opt = np.matmul(self.Uf, g_opt)
        return u_opt, g_opt, t_s
