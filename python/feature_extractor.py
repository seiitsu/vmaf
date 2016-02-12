__copyright__ = "Copyright 2016, Netflix, Inc."
__license__ = "Apache, Version 2.0"

from executor import Executor

class FeatureAssembler(object):
    """
    Assembles features for a input list of Assets on a input list of
    FeatureExtractors, by either retrieve them from a ResultStore, or by
    executing the FeatureExtractors.
    """
    pass

class FeatureExtractor(Executor):
    """
    FeatureExtractor takes in a list of assets, and run feature extraction on
    them, and return a list of corresponding results. A FeatureExtractor must
    specify a unique type and version combination (by the TYPE and VERSION
    attribute), so that the Result generated by it can be identified.
    """

class VmafFeatureExtractor(FeatureExtractor):

    TYPE = "VMAF_feature"
    VERSION = '0.1'
