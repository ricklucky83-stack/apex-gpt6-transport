from __future__ import annotations
import hashlib, json, os, pathlib, subprocess, sys, traceback, zipfile

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def write_json(path,obj):
    pathlib.Path(path).write_text(json.dumps(obj,indent=2,sort_keys=True),encoding='utf-8')

request_path=pathlib.Path(sys.argv[1]).resolve()
out_dir=pathlib.Path(sys.argv[2]).resolve()
out_dir.mkdir(parents=True,exist_ok=True)
req=json.loads(request_path.read_text(encoding='utf-8'))
run_dir=request_path.parent
engine=run_dir/req['engine_file']
player=run_dir/req['player_file']
receipt={
  'status':'STARTED',
  'execution_environment':'GITHUB_ACTIONS',
  'request':req,
  'engine_sha256':sha256(engine),
  'player_sha256':sha256(player),
  'final_board_allowed':False,
}
log=[]
try:
    work=out_dir/'engine'
    work.mkdir(exist_ok=True)
    with zipfile.ZipFile(engine) as z:
        z.extractall(work)
        names=z.namelist()
    receipt['zip_members']=names
    expected='apex_global_production_board_runner_v1.py'
    runner=work/expected
    if not runner.exists():
        receipt.update({
          'status':'ENGINE_PACKAGE_INCOMPLETE',
          'blocking_reason':'PRODUCTION_RUNNER_MISSING_FROM_ENGINE_ZIP',
          'missing_required_file':expected,
          'contract_file_present':(work/'APEX_SIMPLE_ENGINE_CONTRACT_v1.py').exists(),
          'patch_file_present':(work/'apex_global_production_board_runner_v1.patch').exists(),
          'final_board_allowed':False,
        })
        log.append('BLOCKED: engine ZIP contains the contract/patch but not the production runner.')
        log.append(f'MISSING: {expected}')
    else:
        env=os.environ.copy()
        env.update({
          'APEX_SITE':str(req['site']),
          'APEX_SPORT':str(req['sport']),
          'APEX_SLATE_TYPE':str(req['slate_type']),
          'APEX_CONTEST_ID':str(req['contest_id']),
          'APEX_PLAYER_CSV':str(player),
          'APEX_OUTPUT_DIR':str(out_dir),
        })
        p=subprocess.run([sys.executable,str(runner)],cwd=work,env=env,text=True,capture_output=True)
        (out_dir/'engine_stdout.log').write_text(p.stdout,encoding='utf-8')
        (out_dir/'engine_stderr.log').write_text(p.stderr,encoding='utf-8')
        receipt['runner_returncode']=p.returncode
        receipt['status']='COMPLETE' if p.returncode==0 else 'ENGINE_EXECUTION_FAILED'
        receipt['final_board_allowed']=(p.returncode==0 and (out_dir/'FINAL_BOARD.csv').exists())
except Exception as e:
    receipt.update({'status':'HARNESS_ERROR','blocking_reason':repr(e),'final_board_allowed':False})
    log.append(traceback.format_exc())
write_json(out_dir/'RUN_RECEIPT.json',receipt)
(out_dir/'execution.log').write_text('\n'.join(log)+'\n',encoding='utf-8')
if not (out_dir/'FINAL_BOARD.csv').exists():
    (out_dir/'FINAL_BOARD_NOT_PRODUCED.txt').write_text(
      'No final board was produced. See RUN_RECEIPT.json and execution.log.\n',
      encoding='utf-8')
print(json.dumps(receipt,indent=2))
