from __future__ import annotations
import base64, hashlib, json, os, pathlib, subprocess, sys, traceback, zipfile

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def write_json(path,obj):
    pathlib.Path(path).write_text(json.dumps(obj,indent=2,sort_keys=True),encoding='utf-8')

def materialize(run_dir, req, key):
    direct=req.get(f'{key}_file')
    parts=req.get(f'{key}_parts')
    if parts:
        target=run_dir/f'.materialized_{key}'
        data=''.join((run_dir/p).read_text(encoding='ascii').strip() for p in parts)
        target.write_bytes(base64.b64decode(data, validate=True))
        return target
    return run_dir/direct

request_path=pathlib.Path(sys.argv[1]).resolve()
out_dir=pathlib.Path(sys.argv[2]).resolve()
out_dir.mkdir(parents=True,exist_ok=True)
req=json.loads(request_path.read_text(encoding='utf-8'))
run_dir=request_path.parent
log=[]
receipt={'status':'STARTED','execution_environment':'GITHUB_ACTIONS','request':req,'final_board_allowed':False}
try:
    engine=materialize(run_dir,req,'engine')
    player=materialize(run_dir,req,'player')
    receipt['engine_sha256']=sha256(engine)
    receipt['player_sha256']=sha256(player)
    if receipt['engine_sha256'] != req.get('expected_engine_sha256') or receipt['player_sha256'] != req.get('expected_player_sha256'):
        receipt.update({'status':'INPUT_HASH_MISMATCH','blocking_reason':'GITHUB_INPUT_BYTES_DO_NOT_MATCH_SOURCE','final_board_allowed':False})
        log.append('BLOCKED: reconstructed GitHub input hashes do not match source hashes.')
    else:
        receipt['input_hashes_verified']=True
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
            requirements=work/'requirements.txt'
            if requirements.exists():
                dep=subprocess.run(
                    [sys.executable,'-m','pip','install','--disable-pip-version-check','-r',str(requirements)],
                    cwd=work,text=True,capture_output=True
                )
                (out_dir/'dependency_install_stdout.log').write_text(dep.stdout,encoding='utf-8')
                (out_dir/'dependency_install_stderr.log').write_text(dep.stderr,encoding='utf-8')
                receipt['dependency_install_returncode']=dep.returncode
                if dep.returncode != 0:
                    receipt.update({'status':'DEPENDENCY_INSTALL_FAILED','blocking_reason':'ENGINE_REQUIREMENTS_INSTALL_FAILED','final_board_allowed':False})
                    log.append('BLOCKED: engine requirements installation failed.')
                else:
                    receipt['dependencies_installed']=True
            if receipt.get('status') != 'DEPENDENCY_INSTALL_FAILED':
                env=os.environ.copy()
                env.update({
                  'APEX_SITE':str(req['site']),
                  'APEX_SPORT':str(req['sport']),
                  'APEX_SLATE_TYPE':str(req['slate_type']),
                  'APEX_CONTEST_ID':str(req['contest_id']),
                  'APEX_FIELD_SIZE':str(req.get('field_size',req['contest_id'])),
                  'APEX_MC_WORLDS':str(req.get('mc_worlds',200000)),
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
    (out_dir/'FINAL_BOARD_NOT_PRODUCED.txt').write_text('No final board was produced. See RUN_RECEIPT.json and execution.log.\n',encoding='utf-8')
print(json.dumps(receipt,indent=2))
