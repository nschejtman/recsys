import numpy as np
import scipy.sparse as sps
from collections import namedtuple
from sklearn.model_selection import KFold, ParameterGrid
import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.linear_model import ElasticNet, Ridge, Lasso
from sklearn.base import BaseEstimator
import time
import pandas as pd
import sys
sys.path.append('./../')
import utils.utils as ut
from TopPopular.TopPopular import TopPop

def cv_search(rec, urm, non_active_items_mask, sample_size, sample_from_urm=True):
    np.random.seed(1)
    urm_sample, icm_sample, _, non_active_items_mask_sample = ut.produce_sample(urm, icm=None, ucm=None,
                                                                                 non_active_items_mask=non_active_items_mask,
                                                                                 sample_size=sample_size, sample_from_urm=sample_from_urm)
    params = {'l1_ratio':[1e-4, 1e-5, 1e-6, 1e-7]}
    params = {'alpha_ridge':[1e4, 2e4, 5e4, 1e5, 2e5, 5e5]}
    params = {'alpha_ridge': [7000, 8000, 9000]}
    params = {'alpha_ridge':[20000]}
    grid = list(ParameterGrid(params))
    folds = 4
    kfold = KFold(n_splits=folds)
    splits = [(train, test) for train,test in kfold.split(urm_sample)]
    retained_ratings_perc = 0.75
    n = 5
    result = namedtuple('result', ['mean_score', 'std_dev', 'parameters'])
    results = []
    total = float(reduce(lambda acc, x: acc * len(x), params.itervalues(), 1) * folds)
    prog = 1.0
    
    hidden_ratings = []
    for u in range(urm_sample.shape[0]):
        relevant_u = urm_sample[u,].nonzero()[1]  # Indices of rated items for test user u
        if len(relevant_u) > 1:#1 or 2
            np.random.shuffle(relevant_u)
            urm_sample[u, relevant_u[int(len(relevant_u) * retained_ratings_perc):]] = 0
            hidden_ratings.append(relevant_u[int(len(relevant_u) * retained_ratings_perc):])
        else:
            hidden_ratings.append([])

    for pars in grid:
        print pars
        rec = rec.set_params(**pars)
        maps = []
        rec.fit(urm_sample)
        maps.append(ut.map_scorer(rec, urm_sample, hidden_ratings, n, non_active_items_mask_sample))  # Assume rec to predict indices of items, NOT ids
        print "Progress: {:.2f}%".format((prog * 100) / total)
        prog += 1
        print maps
        results.append(result(np.mean(maps), np.std(maps), pars))
        print "Result: ", result(np.mean(maps), np.std(maps), pars)
    scores = pd.DataFrame(data=[[_.mean_score, _.std_dev] + _.parameters.values() for _ in results],
                          columns=["MAP", "Std"] + _.parameters.keys())
    print "Total scores: ", scores
    scores.to_csv('User_SLIM (Ridge) CV MAP values 3.csv', sep='\t', index=False)
    '''cols, col_feat, x_feat = 3, 'l2_penalty', 'l1_penalty'
    f = sns.FacetGrid(data=scores, col=col_feat, col_wrap=cols, sharex=False, sharey=False)
    f.map(plt.plot, x_feat, 'MAP')
    f.fig.suptitle("SLIM-Top pop CV MAP values")
    i_max, y_max = scores['MAP'].argmax(), scores['MAP'].max()
    i_feat_max = params[col_feat].index(scores[col_feat][i_max])
    f_max = f.axes[i_feat_max]
    f_max.plot(scores[x_feat][i_max], y_max, 'o', color='r')
    plt.figtext(0, 0, "With 500 top pops\nMaximum at (sh={:.5f},k={:.5f}, {:.5f}+/-{:.5f})".format(
        scores[col_feat][i_max],
        scores[x_feat][i_max],
        y_max,
        scores['Std'][i_max]))
    plt.tight_layout()
    plt.subplots_adjust(top=0.9, bottom=0.15)
    f.savefig('SLIM_Item CV MAP values 1.png', bbox_inches='tight')'''



class User_SLIM(BaseEstimator):
    """
    Train a Sparse Linear Methods (SLIM) item similarity model.
    See:
        Efficient Top-N Recommendation by Linear Regression,
        M. Levy and K. Jack, LSRS workshop at RecSys 2013.
        SLIM: Sparse linear methods for top-n recommender systems,
        X. Ning and G. Karypis, ICDM 2011.
        http://glaros.dtc.umn.edu/gkhome/fetch/papers/SLIM2011icdm.pdf
    """

    def __init__(self, top_pops, l1_ratio=None, alpha_ridge=None, alpha_lasso=None, pred_batch_size=2500):
        self.l1_ratio = l1_ratio
        self.alpha_ridge = alpha_ridge
        self.alpha_lasso = alpha_lasso
        self.top_pops = top_pops
        self.pred_batch_size = pred_batch_size

    def fit(self, URM):
        print time.time(), ": ", "Started fit"
        URM_T = URM.T.copy()
        URM_T = ut.check_matrix(URM_T, 'csc', dtype=np.float32)
        n_users = URM_T.shape[1]

        # initialize the ElasticNet model
        if self.alpha_ridge is not None:
            self.model = Ridge(self.alpha_ridge, copy_X=False, fit_intercept=False)
        elif self.alpha_lasso is not None:
            self.model = Lasso(alpha=self.alpha_lasso, copy_X=False, fit_intercept=False)
        else:
            self.model = ElasticNet(alpha=1.0, l1_ratio=self.l1_ratio, positive=True, fit_intercept=False, copy_X=False)

        # we'll store the W matrix into a sparse csr_matrix
        # let's initialize the vectors used by the sparse.csc_matrix constructor
        values, rows, cols = [], [], []

        # fit each item's factors sequentially (not in parallel)
        for u in xrange(n_users):
            # print time.time(), ": ", "Started fit > Iteration ", j, "/", n_items
            # get the target column
            y = URM_T[:, u].toarray()
            # set the j-th column of X to zero
            startptr = URM_T.indptr[u]
            endptr = URM_T.indptr[u + 1]
            bak = URM_T.data[startptr: endptr].copy()
            URM_T.data[startptr: endptr] = 0.0
            # fit one ElasticNet model per column
            #print time.time(), ": ", "Started fit > Iteration ", j, "/", n_items, " > Fitting ElasticNet model"
            if self.alpha_ridge is None and self.alpha_lasso is None:
                self.model.fit(URM_T, y)
            else:
                self.model.fit(URM_T, y.ravel())

            # self.model.coef_ contains the coefficient of the ElasticNet model
            # let's keep only the non-zero values
            nnz_mask = self.model.coef_ > 0.0
            values.extend(self.model.coef_[nnz_mask])
            rows.extend(np.arange(n_users)[nnz_mask])
            cols.extend(np.ones(nnz_mask.sum()) * u)
            # print nnz_mask.sum(), (self.model.coef_ > 1e-4).sum()

            # finally, replace the original values of the j-th column
            URM_T.data[startptr:endptr] = bak

        # generate the sparse weight matrix
        self.W_sparse = sps.csc_matrix((values, (rows, cols)), shape=(n_users, n_users), dtype=np.float32)
        print time.time(), ": ", "Finished fit"

    def predict(self, URM, n_of_recommendations=5, non_active_items_mask=None):
        print time.time(), ": ", "Started predict"
        # compute the scores using the dot product

        n_iterations = self.W_sparse.shape[0] / self.pred_batch_size + (self.W_sparse.shape[0] % self.pred_batch_size != 0)
        ranking = None

        for i in range(n_iterations):
            print "Iteration: ", i + 1, "/", n_iterations
            start = i * self.pred_batch_size
            end = start + self.pred_batch_size if i < n_iterations - 1 else self.W_sparse.shape[0]

            batch_users = self.W_sparse[:,start:end].T
            batch_scores = batch_users.dot(URM).toarray().astype(np.float32)

            nonzero_indices = batch_users.nonzero()
            batch_scores[nonzero_indices[0], nonzero_indices[1]] = 0.0

            # remove the inactives items
            batch_scores[:, non_active_items_mask] = 0.0
            batch_ranking = batch_scores.argsort()[:, ::-1]
            batch_ranking = batch_ranking[:, :n_of_recommendations]  # leave only the top n

            sum_of_scores = batch_scores[np.arange(batch_scores.shape[0]), batch_ranking.T].T.sum(axis=1).ravel()
            zero_scores_mask = sum_of_scores == 0
            n_zero_scores = np.extract(zero_scores_mask, sum_of_scores).shape[0]
            if n_zero_scores != 0:
                batch_ranking[zero_scores_mask] = [self.top_pops[:n_of_recommendations] for _ in range(n_zero_scores)]

            if i == 0:
                ranking = batch_ranking.copy()
            else:
                ranking = np.vstack((ranking, batch_ranking))

        print time.time(), ": ", "Finished predict"

        return ranking


urm = ut.read_interactions()

items_dataframe = ut.read_items()
item_ids = items_dataframe.id.values
actives = np.array(items_dataframe.active_during_test.values)
non_active_items_mask = actives == 0
test_users_idx = pd.read_csv('../../inputs/target_users_idx.csv')['user_idx'].values
urm_pred = urm[test_users_idx, :]

top_rec = TopPop(count=True)
top_rec.fit(urm)
top_pops = top_rec.top_pop[non_active_items_mask[top_rec.top_pop] == False]

# TODO: Use all top_pops or only active ones in fitting??
recommender = User_SLIM(top_pops=top_pops, pred_batch_size=1000)
# recommender.fit(urm)
cv_search(recommender, urm, non_active_items_mask, sample_size=None, sample_from_urm=True)