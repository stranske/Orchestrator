import os,sys,pytest
fds=[]
try:
 while not fds or fds[-1] < 1100:
  fds.append(os.open(os.devnull,os.O_RDONLY))
 print('Held descriptors through',fds[-1],flush=True)
 result=pytest.main(['tests/test_mirror_generations.py::MirrorGenerationTests::test_startup_modules_share_the_tick_generation','-q'])
finally:
 for fd in fds:os.close(fd)
sys.exit(result)
