''' Classes for doing Gaussian process models of proteins.'''

from collections import namedtuple
import pickle
import abc

import numpy as np
from scipy.optimize import minimize
from scipy import stats, integrate
import pandas as pd
from sklearn import linear_model

from gpmodel import gpmean
from gpmodel import gpkernel
from gpmodel import gptools
from gpmodel import chimera_tools
from cholesky import chol


class BaseGPModel(abc.ABC):

    """ Base class for Gaussian process models. """

    @abc.abstractmethod
    def __init__(self, kernel):
        self.kernel = kernel

    @abc.abstractmethod
    def predict(self, X):
        return

    @abc.abstractmethod
    def fit(self, X, Y):
        return

    def _set_params(self, **kwargs):
        ''' Sets parameters for the model.

        This function can be used to set the value of any or all
        attributes for the model. However, it does not necessarily
        update dependencies, so use with caution.
        '''
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def load(cls, model):
        ''' Load a saved model.

        Use pickle to load the saved model.

        Parameters:
            model (string): path to saved model
        '''
        with open(model, 'rb') as m_file:
            attributes = pickle.load(m_file, encoding='latin1')
        model = cls(attributes['kernel'])
        del attributes['kernel']
        if attributes['objective'] == 'LOO_log_p':
            model.objective = model._LOO_log_p
        else:
            model.objective = model._log_ML
        del attributes['objective']
        model._set_params(**attributes)
        return model

    def dump(self, f):
        ''' Save the model.

        Use pickle to save a dict containing the model's
        attributes.

        Parameters:
            f (string): path to where model should be saved
        '''
        save_me = {k: self.__dict__[k] for k in list(self.__dict__.keys())}
        if self.objective == self._log_ML:
            save_me['objective'] = 'log_ML'
        else:
            save_me['objective'] = 'LOO_log_p'
        save_me['guesses'] = self.guesses
        try:
            save_me['hypers'] = list(self.hypers)
            # names = self.hypers._fields
            # hypers = {n: h for n, h in zip(names, self.hypers)}
            # save_me['hypers'] = hypers
        except AttributeError:
            pass
        with open(f, 'wb') as f:
            pickle.dump(save_me, f)


class GPRegressor(BaseGPModel):

    """ A Gaussian process regression model for proteins. """

    def __init__(self, kernel, **kwargs):
        BaseGPModel.__init__(self, kernel)
        self.guesses = None
        if 'objective' not in list(kwargs.keys()):
            kwargs['objective'] = 'log_ML'
        if 'mean_func' not in list(kwargs.keys()):
            self.mean_func = gpmean.GPMean()
        self.variances = None
        self._set_objective(kwargs['objective'])
        del kwargs['objective']
        self._set_params(**kwargs)

    def _set_objective(self, objective):
        """ Set objective function for model. """
        if objective is not None:
            if objective == 'log_ML':
                self.objective = self._log_ML
            elif objective == 'LOO_log_p':
                self.objective = self._LOO_log_p
            else:
                raise AttributeError(objective + ' is not a valid objective')
        else:
            self.objective = self._log_ML

    def fit(self, X, Y, variances=None):
        ''' Fit the model to the given data.

        Set the hyperparameters by training on the given data.
        Update all dependent values.

        Measurement variances can be given, or
        a global measurement variance will be estimated.

        Parameters:
            X (np.ndarray): n x d
            Y (np.ndarray): n.
            variances (np.ndarray): n. Optional.
        '''
        if isinstance(X, pd.DataFrame):
            X = X.values
        if isinstance(Y, pd.Series):
            Y = Y.values
        self.X = X
        self.Y = Y
        self._ell = len(Y)
        self._n_hypers = self.kernel.fit(X)
        self.mean, self.std, self.normed_Y = self._normalize(self.Y)
        self.mean_func.fit(X, self.normed_Y)
        self.normed_Y -= self.mean_func.mean(X).T[0]
        if variances is not None:
            if not len(variances) != len(Y):
                raise ValueError('len(variances must match len(Y))')
            self.variances = variances / self.std**2
        else:
            self.variances = None
            self._n_hypers += 1
        if self.guesses is None:
            guesses = [0.9 for _ in range(self._n_hypers)]
        else:
            guesses = self.guesses
            if len(guesses) != self._n_hypers:
                raise AttributeError(('Length of guesses does not match '
                                      'number of hyperparameters'))

        bounds = [(1e-5, None) for _ in guesses]
        minimize_res = minimize(self.objective,
                                (guesses),
                                bounds=bounds,
                                method='L-BFGS-B')
        self.hypers = minimize_res['x']
        if self.objective == self._log_ML:
            self.log_p = self._LOO_log_p(self.hypers)
        else:
            self.ML = self._log_ML(self.hypers)

    def _make_Ks(self, hypers):
        """ Make covariance matrix (K) and noisy covariance matrix (Ky)."""
        if self.variances is not None:
            K = self.kernel.cov(hypers=hypers)
            Ky = K + np.diag(self.variances)
        else:
            K = self.kernel.cov(hypers=hypers[1::])
            Ky = K + np.identity(len(K)) * hypers[0]
        return K, Ky

    def _normalize(self, data):
        """ Normalize the given data.

        Normalizes the elements in data by subtracting the mean and
        dividing by the standard deviation.

        Parameters:
            data (pd.Series)

        Returns:
            mean, standard_deviation, normed
        """
        m = data.mean()
        s = data.std()
        return m, s, (data-m) / s

    def unnormalize(self, normed):
        """ Inverse of _normalize, but works on single values or arrays.

        Parameters:
            normed

        Returns:
            normed*self.std * self.mean
        """
        return normed*self.std + self.mean

    def predict(self, X):
        """ Make predictions for each sequence in new_seqs.

        Predictions are scaled as the original outputs (not normalized)

        Uses Equations 2.23 and 2.24 of RW
        Parameters:
            new_seqs (pd.DataFrame or np.ndarray): sequences to predict.

         Returns:
            means, cov as np.ndarrays. means.shape is (n,), cov.shape is (n,n)
        """
        h = self.hypers[1::]
        if isinstance(X, pd.DataFrame):
            X = X.values
        k_star = self.kernel.cov(X, self.X, hypers=h)
        k_star_star = self.kernel.cov(X, X, hypers=h)
        E = k_star @ self._alpha
        v = np.zeros((len(self.X), len(X)))
        for i in range(len(X)):
            v[:, i] = chol.modified_cholesky_lower_tri_solve(self._L, self._p,
                                                             k_star[i])
        var = k_star_star - v.T @ v
        E += self.mean_func.mean(X)
        E = self.unnormalize(E)
        var *= self.std ** 2
        return E, var

    def _log_ML(self, hypers):
        """ Returns the negative log marginal likelihood for the model.

        Uses RW Equation 5.8.

        Parameters:
            hypers (iterable): the hyperparameters

        Returns:
            log_ML (float)
        """
        self._K, self._Ky = self._make_Ks(hypers)
        self._L, self._p, _ = chol.modified_cholesky(self._Ky)
        self._alpha = chol.modified_cholesky_solve(self._L, self._p,
                                                   self.normed_Y)
        self._alpha = self._alpha.reshape(self._ell, 1)
        first = 0.5 * np.dot(self.normed_Y, self._alpha)
        second = np.sum(np.log(np.diag(self._L)))
        third = len(self._K)/2.*np.log(2*np.pi)
        self.ML = (first+second+third).item()
        return self.ML

    def _LOO_log_p(self, hypers):
        """ Calculates the negative LOO log probability.

        Equation 5.10 and 5.11 from RW
        Parameters:
            variances (iterable)
        Returns:
            log_p
        """
        LOO = self.LOO_res(hypers)
        vs = LOO[:, 1]
        mus = LOO[:, 0]
        log_ps = -0.5*np.log(vs) - (self.normed_Y-mus)**2 / 2 / vs
        log_ps -= 0.5*np.log(2*np.pi)
        return_me = -sum(log_ps)
        return return_me

    def LOO_res(self, hypers, add_mean=False, unnorm=False):
        """ Calculates LOO regression predictions.

        Calculates the LOO predictions according to Equation 5.12 from RW.

        Parameters:
            hypers (iterable)
            add_mean (Boolean): whether or not to add in the mean function.
                Default is False.
            unnormalize (Boolean): whether or not to unnormalize.
                Default is False unless the mean is added back, in which
                case always True.
        Returns:
            res (np.ndarray): columns are 'mu' and 'v'
        """
        K, Ky = self._make_Ks(hypers)
        K_inv = np.linalg.inv(Ky)
        Y = self.normed_Y
        mus = np.diag(Y - np.dot(K_inv, Y) / K_inv)
        vs = np.diag(1 / K_inv)
        if add_mean or unnorm:
            mus = self.unnormalize(mus)
            vs = np.array(vs).copy()
            vs *= self.std**2
        if add_mean:
            mus += self.mean_func.mean(self.X)[:, 0]
        return np.array(list(zip(mus, vs)))

    def score(self, X, Y, *args):
        ''' Score the model on the given points.

        Predicts Y for the sequences in X, then scores the predictions.

        Parameters:
            X (pandas.DataFrame)
            Y (pandas.Series)
            type (string): 'kendalltau', 'R2', or 'R'. Default is 'kendalltau.'

        Returns:
            res: If one score, result is a float. If multiple,
                result is a dict.
        '''
        # Check that X and Y have the same indices
        if not (len(X) == len(Y)):
            raise ValueError
            ('X and Y must be the same length.')
        # Make predictions
        pred_Y, _ = self.predict(X)
        # if nothing specified, return Kendall's Tau
        if not args:
            r1 = stats.rankdata(Y)
            r2 = stats.rankdata(pred_Y)
            return stats.kendalltau(r1, r2).correlation

        scores = {}
        for t in args:
            if t == 'kendalltau':
                r1 = stats.rankdata(Y)
                r2 = stats.rankdata(pred_Y)
                scores[t] = stats.kendalltau(r1, r2).correlation
            elif t == 'R2':
                from sklearn.metrics import r2_score
                scores[t] = r2_score(Y, pred_Y)
            elif t == 'R':
                scores[t] = np.corrcoef(Y, pred_Y[:, 0])[0, 1]
            else:
                raise ValueError('Invalid metric.')
        if len(list(scores.keys())) == 1:
            return scores[list(scores.keys())[0]]
        else:
            return scores


class GPClassifier(BaseGPModel):

    """ A Gaussian process classification model for proteins. """

    def __init__(self, kernel, **kwargs):
        BaseGPModel.__init__(self, kernel)
        self.guesses = None
        self._set_params(**kwargs)
        self.objective = self._log_ML

    def fit(self, X, Y):
        ''' Fit the model to the given data.

        Set the hyperparameters by training on the given data.
        Update all dependent values.

        Parameters:
            X (np.ndarray): Sequences in training set
            Y (np.ndarray): measurements in training set
        '''
        if isinstance(X, pd.DataFrame):
            X = X.values
        if isinstance(Y, pd.Series):
            Y = Y.values
        self.X = X
        self.Y = Y
        self._ell = len(Y)
        self._n_hypers = self.kernel.fit(X)
        if self.guesses is None:
            guesses = [0.9 for _ in range(self._n_hypers)]
        else:
            guesses = self.guesses
            if len(guesses) != self._n_hypers:
                raise AttributeError(('Length of guesses does not match '
                                      'number of hyperparameters'))
        bounds = [(1e-5, None) for _ in guesses]
        minimize_res = minimize(self.objective,
                                (guesses),
                                bounds=bounds,
                                method='L-BFGS-B')
        self.hypers = minimize_res['x']

    def predict(self, X):
        """ Make predictions for each sequence in new_seqs.

        Uses Equations 2.23 and 2.24 of RW
        Parameters:
            new_seqs (np.ndarray): sequences to predict.

         Returns:
            pi_star, f_bar, var as np.ndarrays
        """
        predictions = []
        h = self.hypers
        k_star = self.kernel.cov(X, self.X, hypers=h)
        k_star_star = self.kernel.cov(X, X, hypers=h)
        f_bar = np.dot(k_star, self._grad.T)
        Wk = np.dot(self._W_root, k_star.T)
        v = np.zeros((len(self.X), len(X)))
        for i in range(len(X)):
            v[:, i] = chol.modified_cholesky_lower_tri_solve(self._L, self._p,
                                                             Wk[:, i])
        var = k_star_star - np.dot(v.T, v)
        if self.variances is None:
            var += self.hypers[0]
        span = 20
        pi_star = np.zeros(len(X))
        for i, preds in enumerate(zip(f_bar, np.diag(var))):
            f, va = preds
            pi_star[i] = integrate.quad(self._p_integral,
                                        -span * va + f,
                                        span * va + f,
                                        args=(f, va))[0]
        return pi_star, f_bar, var

    def _p_integral(self, z, mean, variance):
        ''' Equation 3.25 from RW with a sigmoid likelihood.

        Equation to integrate when calculating pi_star for classification.

        Parameters:
            z (float): value at which to evaluate the function.
            mean (float): mean of the Gaussian
            variance (float): variance of the Gaussian

        Returns:
            res (float)
        '''
        try:
            first = 1./(1+np.exp(-z))
        except OverflowError:
            first = 0.
        second = 1 / np.sqrt(2 * np.pi * variance)
        third = np.exp(-(z-mean) ** 2 / (2*variance))
        return first*second*third

    def _log_ML(self, hypers):
        """ Returns the negative log marginal likelihood for the model.

        Uses RW Equation 3.32.

        Parameters:
            hypers (iterable): the hyperparameters

        Returns:
            log_ML (float)
        """
        f_hat = self._find_F(hypers=hypers)
        self.ML = self._logq(f_hat)
        return self.ML

    def _logistic_likelihood(self, Y, F):
        ''' Calculate logistic likelihood.

        Calculates the logistic probability of the outcomes G given
        the latent variables F according to Equation 3.2 in RW.

        Inputs for all the probability functions must be floats or 1D arrays.

        Parameters:
            Y (float or np.ndarray): +/- 1
            F (float or np.ndarray): value of latent function

        Returns:
            float or ndarray
        '''
        if isinstance(Y, np.ndarray):
            if not ((Y == 1).astype(int) + (Y == -1).astype(int)).all():
                raise RuntimeError('All values in Y must be -1 or 1')
        else:
            if int(Y) not in [1, -1]:
                raise RuntimeError('Y must be -1 or 1')
        return 1./(1 + np.exp(-Y * F))

    def _log_logistic_likelihood(self, Y, F):
        """ Calculate the log logistic likelihood.

        Calculates the log logistic likelihood of the outcomes Y
        given the latent variables F. log[p(Y|f)]. Uses Equation
        3.15 of RW.

        Parameters:
            Y (np.ndarray): outputs, +/-1
            F (np.ndarray): values for the latent function

        Returns:
            lll (float): log logistic likelihood
        """
        if isinstance(Y, np.ndarray) and len(Y) != len(F):
            raise RuntimeError('Y and F must be the same length')
        lll = np.sum(np.log(self._logistic_likelihood(Y, F)))
        return lll

    def _grad_log_logistic_likelihood(self, Y, F):
        """ Calculate the gradient of the log logistic likelihood.

        Calculates the gradient of the logistic likelihood of the
        outcomes Y given the latent variables F.
        Uses Equation 3.15 of RW.

        Parameters:
            Y (np.ndarray): outputs, +/-1
            F (np.ndarray): values for the latent function

        Returns:
            glll (np.ndarray): diagonal matrix containing the
                gradient of the log likelihood
        """
        glll = (Y + 1) / 2.0 - self._logistic_likelihood(1.0, F)
        return np.diag(glll)

    def _hess(self, F):
        """ Calculate the negative hessian of the logistic likelihod.

        Calculates the negative hessian of the logistic likelihood
        according to Equation 3.15 of RW.

        Parameters:
            F (np.ndarray): values for the latent function

        Returns:
            W (np.matrix): diagonal negative hessian of the log
                likelihood matrix
        """
        pi = self._logistic_likelihood(1.0, F)
        W = pi * (1 - pi)
        return np.diag(W)

    def _find_F(self, hypers, guess=None, threshold=.0001, evals=1000):
        """Calculates f_hat according to Algorithm 3.1 in RW.

        Returns:
            f_hat (np.ndarray)
        """
        ell = len(self.Y)
        if guess is None:
            f_hat = np.zeros(ell)
        elif len(guess) == l:
            f_hat = guess
        else:
            raise ValueError('Initial guess must have same dimensions as Y')
        self._K = self.kernel.cov(hypers=hypers)
        n_below = 0
        for i in range(evals):
            # find new f_hat
            W = self._hess(f_hat)
            W_root = np.sqrt(W)
            trip_dot = (W_root.dot(self._K)).dot(W_root)
            L, p, _ = chol.modified_cholesky(np.eye(ell) + trip_dot)
            b = W.dot(f_hat.T)
            b += np.diag(self._grad_log_logistic_likelihood(self.Y, f_hat)).T
            b = b.reshape(len(b), 1)
            inside = W_root.dot(self._K).dot(b).reshape(L.shape[0])
            trip_dot_lstsq = chol.modified_cholesky_solve(L, p, inside)\
                .reshape(b.shape)
            a = b - W_root.dot(trip_dot_lstsq)
            f_new = self._K.dot(a)
            f_new = f_new.reshape((len(f_new), ))
            sq_error = np.sum((f_hat - f_new) ** 2)
            if sq_error / abs(np.sum(f_new)) < threshold:
                n_below += 1
            else:
                n_below = 0
            if n_below > 9:
                return f_new
            f_hat = f_new
        raise RuntimeError('Maximum evaluations reached without convergence.')

    def _logq(self, F):
        ''' Calculate negative log marginal likelihood.

        Finds the negative log marginal likelihood for Laplace's
        approximation Equation 5.20 or 3.32 from RW, as described
        in Algorithm 5.1

        Parameters:
            var_p (float)
            F (Series): values for the latent function

        Returns:
            _logq (float)
        '''
        ell = self._ell
        self._W = self._hess(F)
        self._W_root = np.sqrt(self._W)
        F_mat = F.reshape(len(F), 1)
        trip_dot = self._W_root @ self._K @ self._W_root
        self._L, self._p, _ = chol.modified_cholesky(np.eye(ell) + trip_dot)
        b = self._W @ F_mat
        self._grad = np.diag(self._grad_log_logistic_likelihood
                             (self.Y, F))
        b += self._grad.reshape(len(F), 1)
        inside = (self._W_root @ self._K @ b).reshape(self._L.shape[0])
        trip_dot_lstsq = chol.modified_cholesky_solve(self._L, self._p, inside)
        trip_dot_lstsq = trip_dot_lstsq.reshape(b.shape)
        a = b - self._W_root @ trip_dot_lstsq
        _logq = 0.5 * a.T @ F_mat - self._log_logistic_likelihood(
            self.Y, F) + np.sum(np.log(np.diag(self._L)))
        return _logq.item()

    def score(self, X, Y):
        ''' Score the model on the given points.

        Predicts Y for the sequences in X, then scores the predictions.

        Parameters:
            X (pandas.DataFrame)
            Y (pandas.Series)

        Returns:
            res: The auc on the test points.
        '''
        # Check that X and Y have the same indices
        if not (len(X) == len(Y)):
            raise ValueError
            ('X and Y must be the same length.')
        # Make predictions
        p, _, _ = self.predict(X)

        # for classification, return the ROC AUC
        from sklearn.metrics import roc_auc_score
        return roc_auc_score(Y, p)


class GPMultiClassifier(BaseGPModel):

    """ A GP multi-class classifier. """

    def __init__(self, kernels, **kwargs):
        self._kernels = kernels
        self.guesses = None
        self._set_params(**kwargs)
        self.objective = self._log_ML

    def predict(self, X):
        return

    def fit(self, X, Y):
        self.X = X
        self.Y = Y
        self._n_hypers = [k.fit(X) for k in self._kernels]
        return

    def _find_F(self, hypers, guess=None, threshold=1e-12, evals=1000):
        """Calculates f_hat according to Algorithm 3.3 in RW.

        Returns:
            f_hat (np.ndarray): (n_samples x n_classes)
        """
        n_samples, n_classes = self.Y.shape
        Y_vector = (self.Y.T).reshape((n_samples * n_classes, 1))
        if guess is None:
            f_hat = np.zeros_like(self.Y)
        else:
            f_hat = guess
            if guess.shape != self.Y.shape:
                raise ValueError('guess must have same dimensions as Y')
        f_vector = (f_hat.T).reshape((n_samples * n_classes, 1))
        # K[:,:,i] is cov for ith class
        K = self._make_K(hypers=hypers)
        # Block diagonal K
        K_expanded = self._expand(K)
        n_below = 0
        for k in range(evals):
            P = self._softmax(f_hat)
            P_vector = P.T.reshape((n_samples * n_classes, 1))
            PI = self._stack(P)
            E = np.zeros((n_samples, n_samples, n_classes))
            for i in range(n_classes):
                Dc_root = np.sqrt(np.diag(P[:, i]))
                DKD = Dc_root @ K[:, :, i] @ Dc_root
                L = np.linalg.cholesky(np.eye(n_samples) + DKD)
                first = np.linalg.lstsq(L, Dc_root)[0]
                E[:, :, i] = Dc_root @ np.linalg.lstsq(L.T, first)[0]
            M = np.linalg.cholesky(np.sum(E, axis=2))
            D = np.diag(P_vector[:, 0])
            b = (D - PI @ PI.T) @ f_vector + Y_vector - P_vector
            E_expanded = self._expand(E)
            c = E_expanded @ K_expanded @ b
            R = np.concatenate([np.eye(n_samples) for _ in range(n_classes)],
                               axis=0)
            a = b - c
            first, _, _, _ = np.linalg.lstsq(M, R.T @ c)
            a += E_expanded @ R @ np.linalg.lstsq(M.T, first)[0]
            f_vector_new = K_expanded @ a
            sq_error = np.sum((f_vector - f_vector_new) ** 2)
            if sq_error / abs(np.sum(f_vector_new)) < threshold:
                n_below += 1
            else:
                n_below = 0
            if n_below > 9:
                break
            f_vector = f_vector_new
            f_hat = f_vector_new.reshape((n_classes, n_samples)).T
        return f_hat

    def _expand(self, A):
        """ Expand n x m x c matrix to nm x nc block diagonal matrix. """
        n, m, c = A.shape
        expanded = np.zeros((n*c, m*c))
        for i in range(c):
            expanded[i*n:(i+1)*n, i*m:(i+1)*m] = A[:, :, i]
        return expanded

    def _make_K(self, hypers):
        hypers = self._split_hypers(hypers)
        Ks = np.stack([k.cov(hypers=h) for k, h in zip(self._kernels, hypers)],
                      axis=2)
        return Ks

    def _split_hypers(self, hypers):
        inds = np.cumsum(self._n_hypers)
        inds = np.insert(inds, 0, 0)
        return [hypers[inds[i]:inds[i+1]] for i in range(len(inds) - 1)]

    def _softmax(self, f):
        """ Calculate softmaxed probabilities.

        Parameters:
            f (np.ndarray): n_samples x n_classes

        Returns:
            p (np.ndarray): n_samples x n_classes
        """
        P = np.exp(f)
        return P / np.sum(P, axis=1).reshape((P.shape[0], 1))

    def _stack(self, P):
        """ Stack diagonal probability matrices.

        Parameters:
            P(np.ndarray): n_samples x n_classes

        Returns:
            PI(np.ndarray): (n_samples * n_classes) x n_classes
        """
        return np.concatenate([np.diag(p) for p in P.T], axis=0)

    def _log_ML(self, hypers):
        return

class LassoGPRegressor(GPRegressor):

    """ Extends GPRegressor with L1 regression for feature selection. """

    def __init__(self, kernel, **kwargs):
        self._gamma_0 = kwargs.get('gamma', 0)
        self._clf = linear_model.Lasso(alpha=np.exp(self._gamma_0),
                                       warm_start=False,
                                       max_iter=100000)
        GPRegressor.__init__(self, kernel, **kwargs)

    def predict(self, X):
        X, _ = self._regularize(X, mask=self._mask)
        return GPRegressor.predict(self, X)

    def fit(self, X, y, variances=None):
        minimize_res = minimize(self._log_ML_from_gamma,
                                self._gamma_0,
                                args=(X, y, variances),
                                method='Powell',
                                options={'xtol': 1e-8, 'ftol': 1e-8})
        self.gamma = minimize_res['x']

    def _log_ML_from_gamma(self, gamma, X, y, variances=None):
        X, self._mask = self._regularize(X, gamma=gamma, y=y)
        GPRegressor.fit(self, X, y, variances=variances)
        return self.ML

    def _regularize(self, X, **kwargs):
        """ Perform feature selection on X.

        Features can be selected by providing y and gamma, in which
        case L1 linear regression is used to determine which columns
        of X are kept. Or, if a Boolean mask of length equal to the
        number of columns in X is provided, features will be selected
        using the mask.

        Parameters:
            X (pd.DataFrame)

        Optional keyward parameters:
            gamma (float): log amount of regularization
            y (np.ndarray or pd.Series)
            mask (iterable)

        Returns:
            X (pd.DataFrame)
            mask (np.ndarray)
        """
        gamma = kwargs.get('gamma', None)
        y = kwargs.get('y', None)
        mask = kwargs.get('mask', None)
        if gamma is not None:
            if y is None:
                raise ValueError("Missing argument 'y'.")
            self._clf.alpha = np.exp(gamma)
            self._clf.fit(X, y)
            weights = pd.DataFrame()
            weights['weight'] = self._clf.coef_
            mask = ~np.isclose(weights['weight'], 0.0)
        X = X.transpose()[mask].transpose()
        X.columns = list(range(np.shape(X)[1]))
        return X, mask
