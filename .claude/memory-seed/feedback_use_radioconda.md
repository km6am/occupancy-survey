---
name: Use radioconda environment
description: Always activate radioconda conda environment and use conda for package management, not pip
type: feedback
originSessionId: 187bcdf9-1182-4d52-927b-df34f48e1562
---
Always activate the radioconda environment before running commands. Use conda for installing packages, not pip.

**Why:** The project depends on GNU Radio and gr-sdrplay3 which are only available through radioconda. Using pip outside the environment will install to the wrong Python.

**How to apply:** Source `~/radioconda/bin/activate` before running any Python or package install commands. Use `conda install` instead of `pip install`.
