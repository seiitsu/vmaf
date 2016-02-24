__copyright__ = "Copyright 2016, Netflix, Inc."
__license__ = "Apache, Version 2.0"

import config
from executor import Executor
from result import Result
import sys
import numpy as np
from feature_assembler import FeatureAssembler
from train_test_model import LibsvmnusvrTrainTestModel, TrainTestModel
import subprocess
import re

class QualityRunner(Executor):
    """
    QualityRunner takes in a list of assets, and run quality assessment on
    them, and return a list of corresponding results. A QualityRunner must
    specify a unique type and version combination (by the TYPE and VERSION
    attribute), so that the Result generated by it can be identified.

    There are two ways to create a derived class of QualityRunner:

    a) Call a command-line exectuable directly, very similar to what
    FeatureExtractor does. You must:
        1) Override TYPE and VERSION
        2) Override _run_and_generate_log_file(self, asset), which call a
        command-line executable and generate quality scores in a log file.
        3) Override _get_quality_scores(self, asset), which read the quality
        scores from the log file, and return the scores in a dictionary format.
        4) If necessary, override _remove_log(self, asset) if
        Executor._remove_log(self, asset) doesn't work for your purpose
        (sometimes the command-line executable could generate output log files
        in some different format, like multiple files).
    For an example, follow PsnrQualityRunner.

    b) Override the Executor._run_on_asset(self, asset) method to bypass the
    regular routine, but instead, in the method construct a FeatureAssembler
    (which calls a FeatureExtractor (or many) and assembles a list of features,
    followed by using a TrainTestModel (pre-trained somewhere else) to predict
    the final quality score. You must:
        1) Override TYPE and VERSION
        2) Override _run_on_asset(self, asset), which runs a FeatureAssembler,
        collect a feature vector, run TrainTestModel.predict() on it, and
        return a Result object (in this case, both Executor._run_on_asset(self,
        asset) and QualityRunner._read_result(self, asset) get bypassed.
        3) Override _remove_log(self, asset) by redirecting it to the
        FeatureAssembler.
        4) Override _remove_result(self, asset) by redirecting it to the
        FeatureAssembler.
    For an example, follow VmaftQualityRunner.
    """

    def _read_result(self, asset):
        result = {}
        result.update(self._get_quality_scores(asset))
        return Result(asset, self.executor_id, result)

    @classmethod
    def get_scores_key(cls):
        return cls.TYPE + '_scores'

    @classmethod
    def get_score_key(cls):
        return cls.TYPE + '_score'


class PsnrQualityRunner(QualityRunner):

    TYPE = 'PSNR'
    VERSION = '1.0'

    PSNR = config.ROOT + "/feature/psnr"

    def _run_and_generate_log_file(self, asset):
        # routine to call the command-line executable and generate quality
        # scores in the log file.

        log_file_path = self._get_log_file_path(asset)

        # run VMAF command line to extract features, 'APPEND' result (since
        # super method already does something
        quality_width, quality_height = asset.quality_width_height
        psnr_cmd = "{psnr} {yuv_type} {ref_path} {dis_path} {w} {h} >> {log_file_path}" \
        .format(
            psnr=self.PSNR,
            yuv_type=asset.yuv_type,
            ref_path=asset.ref_workfile_path,
            dis_path=asset.dis_workfile_path,
            w=quality_width,
            h=quality_height,
            log_file_path=log_file_path,
        )

        if self.logger:
            self.logger.info(psnr_cmd)

        subprocess.call(psnr_cmd, shell=True)

    def _get_quality_scores(self, asset):
        # routine to read the quality scores from the log file, and return
        # the scores in a dictionary format.

        log_file_path = self._get_log_file_path(asset)

        psnr_scores = []
        counter = 0
        with open(log_file_path, 'rt') as log_file:
            for line in log_file.readlines():
                mo = re.match(r"psnr: ([0-9]+) ([0-9.-]+)", line)
                if mo:
                    cur_idx = int(mo.group(1))
                    assert cur_idx == counter
                    psnr_scores.append(float(mo.group(2)))
                    counter += 1

        assert len(psnr_scores) != 0

        scores_key = self.get_scores_key()
        quality_result = {
            scores_key:psnr_scores
        }
        return quality_result


class VmafQualityRunner(QualityRunner):

    TYPE = 'VMAF'
    VERSION = '0.1'

    FEATURE_ASSEMBLER_DICT = {'VMAF_feature': 'all'}

    FEATURE_RESCALE_DICT = {'VMAF_feature_vif_scores': (0.0, 1.0),
                            'VMAF_feature_adm_scores': (0.4, 1.0),
                            'VMAF_feature_ansnr_scores': (10.0, 50.0),
                            'VMAF_feature_motion_scores': (0.0, 20.0)}

    SVM_MODEL_FILE = config.ROOT + "/resource/model/model_V8a.model"

    # model_v8a.model is trained with customized feature order:
    SVM_MODEL_ORDERED_SCORES_KEYS = ['VMAF_feature_vif_scores',
                                     'VMAF_feature_adm_scores',
                                     'VMAF_feature_ansnr_scores',
                                     'VMAF_feature_motion_scores']

    sys.path.append(config.ROOT + "/libsvm/python")
    import svmutil

    def _get_vmaf_feature_assembler_instance(self, asset):
        vmaf_fassembler = FeatureAssembler(
            feature_dict=self.FEATURE_ASSEMBLER_DICT,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            log_file_dir=self.log_file_dir,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store
        )
        return vmaf_fassembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset), which runs a
        # FeatureAssembler, collect a feature vector, run
        # TrainTestModel.predict() on it, and return a Result object
        # (in this case, both Executor._run_on_asset(self, asset) and
        # QualityRunner._read_result(self, asset) get bypassed.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]

        # =====================================================================

        # SVR predict
        model = self.svmutil.svm_load_model(self.SVM_MODEL_FILE)

        ordered_scaled_scores_list = []
        for scores_key in self.SVM_MODEL_ORDERED_SCORES_KEYS:
            scaled_scores = self._rescale(feature_result[scores_key],
                                          self.FEATURE_RESCALE_DICT[scores_key])
            ordered_scaled_scores_list.append(scaled_scores)

        scores = []
        for score_vector in zip(*ordered_scaled_scores_list):
            vif, adm, ansnr, motion = score_vector
            xs = [[vif, adm, ansnr, motion]]
            score = self.svmutil.svm_predict([0], xs, model)[0][0]
            score = self._post_correction(motion, score)
            scores.append(score)

        result_dict = {}
        # add all feature result
        result_dict.update(feature_result.result_dict)
        # add quality score
        result_dict[self.get_scores_key()] = scores

        return Result(asset, self.executor_id, result_dict)

    def _post_correction(self, motion, score):
        # post-SVM correction
        if motion > 12.0:
            val = motion
            if val > 20.0:
                val = 20
            score *= ((val - 12) * 0.015 + 1)
        if score > 100.0:
            score = 100.0
        elif score < 0.0:
            score = 0.0
        return score

    @classmethod
    def _rescale(cls, vals, lower_upper_bound):
        lower_bound, upper_bound = lower_upper_bound
        vals = np.double(vals)
        vals = np.clip(vals, lower_bound, upper_bound)
        vals = (vals - lower_bound) / (upper_bound - lower_bound)
        return vals

    # override
    def _remove_log(self, asset):
        # Override Executor._remove_log(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_logs()

    # override
    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()

class VmaftQualityRunner(QualityRunner):
    TYPE = 'VMAFT'
    VERSION = '0.1'

    FEATURE_ASSEMBLER_DICT = {'VMAF_feature': 'all'}

    SVM_MODEL_FILE = config.ROOT + "/resource/model/model_v9.model"

    def _get_vmaf_feature_assembler_instance(self, asset):
        vmaf_fassembler = FeatureAssembler(
            feature_dict=self.FEATURE_ASSEMBLER_DICT,
            feature_option_dict=None,
            assets=[asset],
            logger=self.logger,
            log_file_dir=self.log_file_dir,
            fifo_mode=self.fifo_mode,
            delete_workdir=self.delete_workdir,
            result_store=self.result_store
        )
        return vmaf_fassembler

    def _run_on_asset(self, asset):
        # Override Executor._run_on_asset(self, asset), which runs a
        # FeatureAssembler, collect a feature vector, run
        # TrainTestModel.predict() on it, and return a Result object
        # (in this case, both Executor._run_on_asset(self, asset) and
        # QualityRunner._read_result(self, asset) get bypassed.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.run()
        feature_result = vmaf_fassembler.results[0]

        xs = LibsvmnusvrTrainTestModel.get_perframe_xs_from_result(feature_result)

        model = LibsvmnusvrTrainTestModel.from_file(self.SVM_MODEL_FILE, None)

        ys_pred = model.predict(xs)
        ys_pred = np.clip(ys_pred, 0.0, 100.0)

        result_dict = {}
        # add all feature result
        result_dict.update(feature_result.result_dict)
        # add quality score
        result_dict[self.get_scores_key()] = ys_pred

        return Result(asset, self.executor_id, result_dict)

    def _remove_log(self, asset):
        # Override Executor._remove_log(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_logs()

    def _remove_result(self, asset):
        # Override Executor._remove_result(self, asset) by redirecting it to the
        # FeatureAssembler.

        vmaf_fassembler = self._get_vmaf_feature_assembler_instance(asset)
        vmaf_fassembler.remove_results()

