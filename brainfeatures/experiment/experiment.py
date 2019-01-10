from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score
from joblib import Parallel, delayed
from datetime import datetime, date
from collections import OrderedDict
from functools import partial
import logging
import time

from brainfeatures.feature_generation.generate_features import \
    generate_features_of_one_file, default_feature_generation_params
from brainfeatures.analysis.analyze import analyze_quality_of_predictions, \
    analyze_feature_importances, analyze_feature_correlations, \
    analyze_pca_components

from brainfeatures.decoding.decode import validate, final_evaluate

# TODO: move agg mode out of feature generators to experiment? -> moves a lot of data around
# TODO: free memory? how much memory is needed?
# TODO: split devel into train/test before cleaning/feature generation?
# TODO: prune variables visible for user?


class Experiment(object):
    """
    Class that performs one feature-based experiment on development (and
    evaluation) set.

    It is structured as follows:

    1. (optional) Read raw data from given data set(s) and apply given
                  cleaning rules.
    2. (optional) Take previously cleaned signals / read cleaned signals from
                  given data set(s) and apply given feature generation
                  procedure.
    3. (optional) Take previously generated features / read generated features
                  from given data set(s) and run (cross)-validation or final
                  evaluation using given classifier and evaluate it using
                  given metric(s).

    Parameters
    ----------
    devel_set: :class:`.DataSet`
        with __len__ and __getitem__ returning (example, sfreq, label),
    clf:  object, optional
        a classifier following scikit-learn api
    metrics: object, list of objects, optional
         metric(s) following scikit-learn api
    eval_set: :class:`.DataSet`
        with __len__ and __getitem__ returning (example, sfreq, label),
        if None, experiment will perform (cross-)validation,
        if not None, experiment will perform final evaluation
    n_jobs: int, optional
        number of jobs to use for parallel cleaning / feature generation
    cleaning_procedure: function, optional
        takes signals and sampling frequency of the signals and returns cleaned
        signals
    cleaning_params: dict, optional
        keyword arguments needed for cleaning functions except signals and
        sampling frequency in the form
    feature_generation_procedure: function, optional
    feature_generation_params: dict, optional
        keyword arguments needed for feature generation functions except
        signals and sampling frequency
    n_splits_or_repetitions: int, optional
        number of (cross-)validation splits / final evaluation repetitions
    shuffle_splits: bool, optional
        shuffles the cross-validation splits
    pca_thresh: integer, float, optional
        inter specifying number of components to keep / float specifying
        percentage of explained variance to determine number of components to
        keep with application of principal component analysis
    scaler: object, optional
        a scaler following scikit-learn api used to scale feature values
    verbosity: str, optional
        verbosity level
    """
    def __init__(self, devel_set, clf=RandomForestClassifier(n_estimators=100),
                 metrics=accuracy_score, eval_set=None, n_jobs: int=1,
                 cleaning_procedure: callable=None, cleaning_params: dict=None,
                 feature_generation_procedure: callable=generate_features_of_one_file,
                 feature_generation_params: dict=default_feature_generation_params,
                 n_splits_or_repetitions: int=5, shuffle_splits: bool=False,
                 pca_thresh: float=None, scaler=StandardScaler(),
                 feature_vector_modifier: callable=None, verbosity: str="INFO"):

        self._data_sets = OrderedDict([("devel", devel_set), ("eval", eval_set)])
        self._feature_generation_procedure = feature_generation_procedure
        self._feature_generation_params = feature_generation_params
        self._n_splits_or_repetitions = n_splits_or_repetitions
        self._cleaning_procedure = cleaning_procedure
        self._cleaning_params = cleaning_params
        self._shuffle_splits = shuffle_splits
        self._pca_thresh = pca_thresh
        self._verbosity = verbosity
        self._metrics = metrics
        self._n_jobs = n_jobs
        self._scaler = scaler
        self._clf = clf

        self.features = {"devel": [], "eval": []}
        # self.targets = {"devel": [], "eval": []}
        self.clean = {"devel": [], "eval": []}
        self.info = {"devel": {}, "eval": {}}
        self._feature_vector_modifier = feature_vector_modifier
        self.feature_labels = None
        self.performances = {}
        self.predictions = {}
        self.times = {}

    def _run_checks(self):
        """
        Assure conformity of given arguments.
        """
        assert not (self._cleaning_procedure is None
                    and self._feature_generation_procedure is None
                    and self._clf is None), "please specify what to do"
        assert self._verbosity in ["DEBUG", "INFO", "WARNING", "ERROR",
                                   0, 10, 20, 30, 40], "unknown verbosity level"
        if self._feature_generation_procedure is not None:
            assert hasattr(self._feature_generation_procedure, "__call__"), \
                "feature_generation_procedure has to be a callable"
        if self._cleaning_procedure is not None:
            assert hasattr(self._cleaning_procedure, "__call__"), \
                "cleaning_procedure has to be a callable"
        if self._cleaning_params is not None:
            assert type(self._cleaning_params) is dict, \
                "cleaning_params has to be a dictionary"
        if self._feature_generation_params is not None:
            assert type(self._feature_generation_params) is dict, \
                "feature_generation_params has to be a dictionary"
        assert len(self._data_sets["devel"][0]) == 3, \
            "__getitem__ of data set needs to return x, fs, y"
        assert self._shuffle_splits in [True, False], \
            "shuffle_splits has to be boolean"
        assert type(self._n_splits_or_repetitions) is int and \
            self._n_splits_or_repetitions > 0, \
            "n_repetitions has to be an integer larger than 0"
        if self._data_sets["eval"] is None:
            assert self._n_splits_or_repetitions >= 2, \
                "need at least two splits for cv"
        assert type(self._n_jobs) is int and self._n_jobs >= -1, \
            "n_jobs has to be an integer larger or equal to -1"
        if self._feature_vector_modifier is not None:
            assert callable(self._feature_vector_modifier), \
                "modifier has to be a callable"
        if hasattr(self._clf, "n_jobs"):
            self._clf.n_jobs = self._n_jobs
        if self._metrics is not None and not hasattr(self._metrics, "__len__"):
            self._metrics = [self._metrics]
        if self._scaler is not None:
            scaling_functions = ["fit_transform", "transform"]
            for scaling_function in scaling_functions:
                assert hasattr(self._scaler, scaling_function), \
                    "scaler is not following scikit-learn api ({})" \
                    .format(scaling_function)
        if self._clf is not None:
            decoding_functions = ["fit", "predict"]
            for decoding_function in decoding_functions:
                assert hasattr(self._clf, decoding_function), \
                    "classifier is not following scikit-learn api ({})"\
                    .format(decoding_function)
        if self._pca_thresh:
            assert type(self._pca_thresh) in [int, float], \
                "pca_thresh has to be either int or float"
            if self._scaler is None:
                logging.warning("using pca on unscaled features")
        if "eval" in self._data_sets and self._data_sets["eval"] is None:
            self._data_sets.pop("eval")
            self.features.pop("eval")
            # self.targets.pop("eval")
            self.clean.pop("eval")
            self.info.pop("eval")

    def _clean(self, devel_or_eval):
        """
        Apply given cleaning rules to all examples in data set specified by
        devel_or_eval.

        Parameters
        ----------
        devel_or_eval: str
            either "devel" or "eval"
        """
        start = time.time()
        logging.info("Making clean ({})".format(devel_or_eval))
        if self._cleaning_params is not None:
            self.cleaning_procedure = partial(self.cleaning_procedure,
                                              **self._cleaning_params)
        cleaned_signals_and_sfreq = Parallel(n_jobs=self._n_jobs)\
            (delayed(self.cleaning_procedure)(example, sfreq)
                for (example, sfreq, label) in self._data_sets[devel_or_eval])
        # not nice, iterating twice
        for (cleaned_signals, sfreq) in cleaned_signals_and_sfreq:
            if "sfreq" not in self.info[devel_or_eval]:
                self.info[devel_or_eval]["sfreq"] = sfreq
            self.clean[devel_or_eval].append(cleaned_signals)
        # self.targets[devel_or_eval] = self._data_sets[devel_or_eval].targets
        self.times.setdefault("cleaning", {}).update(
            {devel_or_eval: time.time() - start})

    def _load_cleaned_or_features(self, devel_or_eval, clean_or_features):
        """
        Load cleaned signals or features from data set specified by
        devel_or_eval.

        Parameters
        ----------
        devel_or_eval: str
            either "devel" or "eval"
        clean_or_features: str
            either "clean" or "features"
        """
        start = time.time()
        logging.info("Loading {} ({})".format(devel_or_eval,
                                              clean_or_features))
        for (data, sfreq, label) in self._data_sets[devel_or_eval]:
            if "sfreq" not in self.info[devel_or_eval]:
                self.info[devel_or_eval]["sfreq"] = sfreq
            if self.feature_labels is None:
                self.feature_labels = list(data.columns)
            getattr(self, clean_or_features)[devel_or_eval].append(data)
            # self.targets[devel_or_eval].append(label)
        self.times.setdefault("loading", {}).update(
            {devel_or_eval: time.time() - start})

    def _generate_features(self, devel_or_eval):
        """
        Apply given feature generation procedure to all examples in data set
        specified by devel_or_eval.

        Parameters
        ----------
        devel_or_eval: str
            either "devel" or "eval"
        """
        start = time.time()
        logging.info("Generating features ({})".format(devel_or_eval))
        if self._feature_generation_params is not None:
            self.feature_generation_procedure = partial(
                self.feature_generation_procedure,
                **self._feature_generation_params)

        feature_vectors = Parallel(n_jobs=self._n_jobs)\
            (delayed(self.feature_generation_procedure)
                (example, self.info[devel_or_eval]["sfreq"])
                for example in self.clean[devel_or_eval])

        for i, feature_vector in enumerate(feature_vectors):
            if feature_vector is not None:
                if self.feature_labels is None:
                    self.feature_labels = list(feature_vector.columns)
                self.features[devel_or_eval].append(feature_vector.values)
            # important: if feature generation fails, and therefore feature
            # vector is None remove according label!
            else:
                del self._data_sets[devel_or_eval].targets[i]
                # del self.targets[devel_or_eval][i]
                logging.warning("removed example {} from labels".format(i))
        assert len(self.features[devel_or_eval]) == \
            len(self._data_sets[devel_or_eval].targets), \
            "number of feature vectors does not match number of labels"
        self.times.setdefault("feature generation", {}).update(
            {devel_or_eval: time.time() - start})

    def _validate(self):
        """
        Perform (cross-)validation on development set.
        """
        start = time.time()
        logging.info("Making predictions (validation)")
        assert len(self.features["devel"]) == len(self._data_sets["devel"].targets), \
            "number of devel examples and labels differs!"
        validation_results, info = validate(
            self.features["devel"], self._data_sets["devel"].targets, self._clf,
            self._n_splits_or_repetitions, self._shuffle_splits, self._scaler,
            self._pca_thresh)
        self.predictions.update(validation_results)
        if "pca_components" in info["valid"]:
            info["valid"]["pca_components"].columns = self.feature_labels + ["id"]
        elif "feature_importances" in info["valid"]:
            info["valid"]["feature_importances"].columns = self.feature_labels
        self.info.update(info)
        self.times["validation"] = time.time() - start

    def _analyze_performance(self, devel_or_eval):
        """
        Apply specified metrics on predictions on data set specified by
        devel_or_eval.

        Parameters
        ----------
        devel_or_eval: str
            either "devel" or "eval"
        """
        if devel_or_eval == "devel":
            valid_or_final_evaluation = "validation"
        else:
            valid_or_final_evaluation = "final evaluation"
        logging.info("Computing performances ({})".format(
            valid_or_final_evaluation))
        for train_or_devel in ["train", "devel"]:
            if train_or_devel == "devel":
                train_or_devel = "valid"
            performances = analyze_quality_of_predictions(
                self.predictions[train_or_devel], self._metrics)
            self.performances.update({train_or_devel: performances})
            logging.info("Achieved in average\n{}\n on {} set.".format(
                self.performances[train_or_devel].mean().to_string(), train_or_devel))

    def _final_evaluate(self):
        """
        Perform final evaluation on development and final evaluation set.
        """
        start = time.time()
        logging.info("Making predictions (final evaluation)")
        assert len(self.features["eval"]) == len(self._data_sets["eval"].targets), \
            "number of eval examples and labels differs!"
        evaluation_results, eval_info = final_evaluate(
            self.features["devel"], self._data_sets["devel"].targets, self.features["eval"],
            self._data_sets["eval"].targets, self._clf, self._n_splits_or_repetitions,
            self._scaler, self._pca_thresh)
        self.predictions.update(evaluation_results)
        self.info.update(eval_info)
        self.times["final evaluation"] = time.time() - start

    def _run_valid_or_eval(self, devel_or_eval):
        # TODELAY: impove feature vector modifier
        if self._feature_vector_modifier is not None:
            self.features[devel_or_eval], self.feature_labels =\
                self._feature_vector_modifier(self._data_sets[devel_or_eval],
                                              self.features[devel_or_eval],
                                              self.feature_labels)
            assert len(self.features[devel_or_eval]) > 0, \
                "removed all feature vectors"
            assert self.features[devel_or_eval][0].shape[-1] == len(self.feature_labels), \
                "number of features and feature labels does not match"
        if devel_or_eval == "devel":
            self._validate()
        else:
            self._final_evaluate()
        if self._metrics is not None:
            self._analyze_performance(devel_or_eval)

    def run(self):
        """
        Run complete experiment.
        """
        log = logging.getLogger()
        log.setLevel("INFO")
        today, now = date.today(), datetime.time(datetime.now())
        logging.info('Started on {} at {}'.format(today, now))

        self._run_checks()
        do_clean = self._cleaning_procedure is not None
        do_features = self._feature_generation_procedure is not None
        do_predictions = (self._clf is not None and self.features["devel"])
        for devel_or_eval in self._data_sets.keys():
            if do_clean:
                self._clean(devel_or_eval)

            if do_features:
                if not do_clean:
                    self._load_cleaned_or_features(devel_or_eval, "clean")
                self._generate_features(devel_or_eval)

            if not do_clean and not do_features:
                self._load_cleaned_or_features(devel_or_eval, "features")

            if do_predictions:
                self._run_valid_or_eval(devel_or_eval)

        today, now = date.today(), datetime.time(datetime.now())
        logging.info("Finished on {} at {}.".format(today, now))

    # TODO: add a plotting/analysis function?
    def plot(self, out_dir=None):
        """
        Perform analysis of features.
        """
        # do we make analysis on eval set? seems wrong
        # always analyze correlation of features
        analyze_feature_correlations(self.features["devel"],
                                     self.feature_labels,
                                     out_dir)

        # if using random forest (default), analyze its feature_importances
        feature_importances = []
        for info in self.info["devel"]:
            if "clf" in info and hasattr(info["clf"], "feature_importances_"):
                feature_importances.append(
                    getattr(info["clf"], "feature_importances_"))

        if feature_importances:
            analyze_feature_importances(
                feature_importances,
                self.feature_labels,
                out_dir)

        # if pca was used, analyze the components
        pca_components = []
        for info in self.info["devel"]:
            if "pca" in info:
                pca_components.append(getattr(info["pca"], "components_"))

        if pca_components:
            analyze_pca_components(pca_components, self.feature_labels)

        # TODO: generate confusioin matrices, roc auc score, roc auc curves
        # raise NotImplementedError
