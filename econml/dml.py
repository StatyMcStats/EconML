# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Double ML.

"Double Machine Learning" is an algorithm that applies arbitrary machine learning methods
to fit the treatment and response, then uses a linear model to predict the response residuals
from the treatment residuals.

"""

import numpy as np
import copy
from .utilities import shape, reshape, ndim, hstack, cross_product, transpose
from sklearn.model_selection import KFold
from sklearn.linear_model import LinearRegression, LassoCV
from sklearn.preprocessing import PolynomialFeatures, FunctionTransformer
from sklearn.base import clone
from sklearn.pipeline import Pipeline
from .cate_estimator import LinearCateEstimator


class _RLearner(LinearCateEstimator):
    """
    Base class for orthogonal learners.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features and controls. Must implement
        `fit` and `predict` methods.  Unlike sklearn estimators both methods must
        take an extra second argument (the controls).

    model_t: estimator
        The estimator for fitting the treatment to the features and controls. Must implement
        `fit` and `predict` methods.  Unlike sklearn estimators both methods must
        take an extra second argument (the controls).

    model_final: estimator for fitting the response residuals to the features and treatment residuals
        Must implement `fit` and `predict` methods. Unlike sklearn estimators the fit methods must
        take an extra second argument (the treatment residuals).  Predict, on the other hand,
        should just take the features and return the constant marginal effect.

    n_splits: int, optional (default is 2)
        The number of splits to use when fitting the first-stage models.

    """

    def __init__(self, model_y, model_t, model_final, n_splits=2):
        self._models_y = [clone(model_y, safe=False) for _ in range(n_splits)]
        self._models_t = [clone(model_t, safe=False) for _ in range(n_splits)]
        self._model_final = clone(model_final, safe=False)
        self._n_splits = n_splits

    def fit(self, Y, T, X=None, W=None):
        if X is None:
            X = np.ones((shape(Y)[0], 1))
        if W is None:
            W = np.empty((shape(Y)[0], 0))
        assert shape(Y)[0] == shape(T)[0] == shape(X)[0] == shape(W)[0]

        y_res = np.zeros(shape(Y))
        t_res = np.zeros(shape(T))
        for idx, (train_idxs, test_idxs) in enumerate(KFold(self._n_splits).split(X)):
            Y_train, Y_test = Y[train_idxs], Y[test_idxs]
            T_train, T_test = T[train_idxs], T[test_idxs]
            X_train, X_test = X[train_idxs], X[test_idxs]
            W_train, W_test = W[train_idxs], W[test_idxs]
            # TODO: If T is a vector rather than a 2-D array, then the model's fit must accept a vector...
            #       Do we want to reshape to an nx1, or just trust the user's choice of input?
            #       (Likewise for Y below)
            self._models_t[idx].fit(X_train, W_train, T_train)
            t_res[test_idxs] = T_test - self._models_t[idx].predict(X_test, W_test)
            self._models_y[idx].fit(X_train, W_train, Y_train)
            y_res[test_idxs] = Y_test - self._models_y[idx].predict(X_test, W_test)

        self._model_final.fit(X, t_res, y_res)

    def const_marginal_effect(self, X=None):
        """
        Calculate the constant marginal CATE θ(·).

        The marginal effect is conditional on a vector of
        features on a set of m test samples {Xᵢ}.

        Parameters
        ----------
        X: optional (m × dₓ) matrix
            Features for each sample.
            If X is None, it will be treated as a column of ones with a single row

        Returns
        -------
        theta: (m × d_y × dₜ) matrix
            Constant marginal CATE of each treatment on each outcome for each sample.
            Note that when Y or T is a vector rather than a 2-dimensional array,
            the corresponding singleton dimensions in the output will be collapsed
            (e.g. if both are vectors, then the output of this method will also be a vector)
        """
        if X is None:
            X = np.ones((1, 1))
        return self._model_final.predict(X)


class _DMLCateEstimatorBase(_RLearner):
    """
    The base class for Double ML estimators.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.  Must be a linear model for correctness when sparseLinear is `True`.

    model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods.  Must be a linear model for correctness when sparseLinear is `True`.

    model_final: estimator, optional (default is `LinearRegression(fit_intercept=False)`)
        The estimator for fitting the response residuals to the treatment residuals. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    featurizer: transformer, optional (default is `PolynomialFeatures(degree=1, include_bias=True)`)
        The transformer used to featurize the raw features when fitting the final model.  Must implement
        a `fit_transform` method.

    sparseLinear: bool
        Whether to use sparse linear model assumptions

    n_splits: int, optional (default is 2)
        The number of splits to use when fitting the first-stage models.
    """

    def __init__(self,
                 model_y, model_t, model_final,
                 featurizer,
                 sparseLinear,
                 n_splits):
        featurizer = clone(featurizer, safe=False)
        model_final = clone(model_final)

        class FirstStageWrapper:
            def __init__(self, model, is_Y):
                self._model = model
                self._is_Y = is_Y

            def __deepcopy__(self, memo):
                return FirstStageWrapper(clone(self._model), self._is_Y)

            def _combine(self, X, W):
                if self._is_Y and sparseLinear:
                    XW = hstack([X, W])
                    F = featurizer.fit_transform(X)
                    return cross_product(XW, hstack([np.ones((shape(XW)[0], 1)), F, W]))
                else:
                    return hstack([featurizer.fit_transform(X), W])

            def fit(self, X, W, Target):
                self._model.fit(self._combine(X, W), Target)

            def predict(self, X, W):
                return self._model.predict(self._combine(X, W))

        class FinalWrapper:
            def fit(self, X, T_res, Y_res):
                # Track training dimensions to see if Y or T is a vector instead of a 2-dimensional array
                self._d_t = shape(T_res)[1:]
                self._d_y = shape(Y_res)[1:]

                model_final.fit(cross_product(featurizer.fit_transform(X), T_res), Y_res)

            def predict(self, X):
                # create an identity matrix of size d_t (or just a 1-element array if T was a vector)
                # the nth row will allow us to compute the marginal effect of the nth component of treatment
                eye = np.eye(self._d_t[0]) if self._d_t else np.array([1])
                # TODO: Doing this kronecker/reshaping/transposing stuff so that predict can be called
                #       rather than just using coef_ seems silly, but one benefit is that we can use linear models
                #       that don't expose a coef_ (e.g. a GridSearchCV over underlying linear models)
                flat_eye = reshape(eye, (1, -1))
                XT = reshape(np.kron(flat_eye, featurizer.fit_transform(X)),
                             ((self._d_t[0] if self._d_t else 1) * shape(X)[0], -1))
                effects = reshape(model_final.predict(XT), (-1,) + self._d_t + self._d_y)
                if self._d_t and self._d_y:
                    return transpose(effects, (0, 2, 1))  # need to return as m by d_y by d_t matrix
                else:
                    return effects

            @property
            def coef_(self):
                # TODO: handle case where final model doesn't directly expose coef_?
                return reshape(model_final.coef_, self._d_y + self._d_t + (-1,))

        super().__init__(model_y=FirstStageWrapper(model_y, is_Y=True),
                         model_t=FirstStageWrapper(model_t, is_Y=False),
                         model_final=FinalWrapper(),
                         n_splits=n_splits)

    @property
    def coef_(self):
        """
        Get the final model's coefficients.

        Note that this relies on the final model having a `coef_` property of its own.
        Most sklearn linear models support this, but there are cases that don't
        (e.g. a `Pipeline` or `GridSearchCV` which wraps a linear model)
        """
        return self._model_final.coef_


class DMLCateEstimator(_DMLCateEstimatorBase):
    """
    The Double ML Estimator.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.

    model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods.

    model_final: estimator, optional (default is `LinearRegression(fit_intercept=False)`)
        The estimator for fitting the response residuals to the treatment residuals. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    featurizer: transformer, optional (default is `PolynomialFeatures(degree=1, include_bias=True)`)
        The transformer used to featurize the raw features when fitting the final model.  Must implement
        a `fit_transform` method.

    n_splits: int, optional (default is 2)
        The number of splits to use when fitting the first-stage models.

    """

    def __init__(self,
                 model_y, model_t, model_final=LinearRegression(fit_intercept=False),
                 featurizer=PolynomialFeatures(degree=1, include_bias=True),
                 n_splits=2):
        super().__init__(model_y=model_y,
                         model_t=model_t,
                         model_final=model_final,
                         featurizer=featurizer,
                         sparseLinear=False,
                         n_splits=n_splits)


class SparseLinearDMLCateEstimator(_DMLCateEstimatorBase):
    """
    A specialized version of the Double ML estimator for the sparse linear case.

    Specifically, this estimator can be used when the controls are high-dimensional,
    the treatment and response are linear functions of the features and controls,
    and the coefficients of the nuisance functions are sparse.

    Parameters
    ----------
    linear_model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    linear_model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    model_final: estimator, optional (default is `LinearRegression(fit_intercept=False)`)
        The estimator for fitting the response residuals to the treatment residuals. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    featurizer: transformer, optional (default is `PolynomialFeatures(degree=1, include_bias=True)`)
        The transformer used to featurize the raw features when fitting the final model.  Must implement
        a `fit_transform` method.

    n_splits: int, optional (default is 2)
        The number of splits to use when fitting the first-stage models.
    """

    def __init__(self,
                 linear_model_y=LassoCV(), linear_model_t=LassoCV(), model_final=LinearRegression(fit_intercept=False),
                 featurizer=PolynomialFeatures(degree=1, include_bias=True),
                 n_splits=2):
        super().__init__(model_y=linear_model_y,
                         model_t=linear_model_t,
                         model_final=model_final,
                         featurizer=featurizer,
                         sparseLinear=True,
                         n_splits=n_splits)


class KernelDMLCateEstimator(DMLCateEstimator):
    """
    A specialized version of the Double ML Estimator that uses random fourier features.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.

    model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods.

    model_final: estimator, optional (default is `LinearRegression(fit_intercept=False)`)
        The estimator for fitting the response residuals to the treatment residuals. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    dim: int, optional (default is 20)
        The number of random Fourier features to generate

    bw: float, optional (default is 1.0)
        The bandwidth of the Gaussian used to generate features

    n_splits: int, optional (default is 2)
        The number of splits to use when fitting the first-stage models.

    """

    def __init__(model_y, model_t, model_final=LinearRegression(fit_intercept=False), dim=20, bw=1.0, n_splits=2):
        class RandomFeatures:
            def __init__(self):
                self.omegas = defaultdict(lambda d_x: np.random.normal(0, 1 / bw, size=(d_x, dim)))
                self.biases = defaultdict(lambda d_x: np.random.uniform(0, 2 * np.pi, size=(1, dim)))

            def fit_transform(self, X):
                omegas = self.omegas[shape(X)[1]]
                biases = self.biases[shape(X)[1]]
                return np.sqrt(2 / dim) * np.cos(np.matmul(X, omegas) + biases)
        super().__init__(model_y, model_t, model_final, RandomFeatures(), n_splits)
