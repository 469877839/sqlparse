# Formatting conservation failures

`tests/test_format_conservation.py` drops one JSON file per failing
(input, options) pair here.  Each file contains the original SQL, the
format options, the formatted output, the semantic token streams before
and after, and a unified token diff -- everything needed to paste the
case into the regression suite.  Files are named by content hash, so a
recurring failure always maps to the same file.  This directory is
expected to be empty when the suite is green.
