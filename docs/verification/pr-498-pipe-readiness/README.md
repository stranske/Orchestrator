# PR498 pipe readiness recovery

Hosted Python3.13 run37564941507/job112610504275 reported six startup-generation subtest failures, all `ValueError: filedescriptor out of range in select()` on exact head45792deb96ccc5c8ec4ea03fb5372023a9f76ee2. Holding descriptors through1100 reproduced all six failures on that exact revision. Replacing the five test pipe waits with a context-managed DefaultSelector removes FD_SETSIZE while retaining each existing timeout/read/cleanup assertion. Production code is unchanged.

Control command: `/opt/anaconda3/bin/python3 work/highfd.py` where the driver holds os.open(os.devnull,os.O_RDONLY) descriptors through1100, calls pytest.main(['tests/test_mirror_generations.py::MirrorGenerationTests::test_startup_modules_share_the_tick_generation','-q']), and closes every held descriptor in finally. Original source:6FAILED/1PASSED,exit1. Repaired source:1PASSED/6subtestsPASSED,exit0. Complete driver is adjacent.

Normal module: `/opt/anaconda3/bin/python3 -m pytest tests/test_mirror_generations.py -q`:14PASSED/14subtestsPASSED. Black/Ruff/diff pass. After integrating main508(3a14ae6), the same descriptor-pressure control passes. Exact pytest collect-only measures2624nodes; floor reconciled to that exact count, with every skip/type ceiling unchanged. These results are focused execution and collection evidence; broader local/hosted suites are separate, and the actual two-profile by three-attempt measured trial remains incomplete. No provider identity/usage/output or Brain promotion is inferred.

Actual local Python3.13.9/pytest9.1.1 also executes the same integrated descriptor-pressure control:1PASSED/6subtestsPASSED,exit0. This is the hosted failing Python version, exercised locally; fresh hosted complete checks remain required.
