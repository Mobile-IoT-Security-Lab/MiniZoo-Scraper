"""Vendored pure static reader subset; never evaluates package code."""

import os, stat, json, hashlib, struct, re, time, collections

from pathlib import PurePosixPath

VERSION='static-wxapkg-metadata-reader/1.0.0'

MAX_PACKAGE=256*1024*1024

MAX_ENTRIES=100000

MAX_JSON=16*1024*1024

MAX_EVIDENCE=100000

WX=re.compile(rb'(?<![A-Za-z0-9_])wx[0-9a-fA-F]{16}(?![A-Za-z0-9_])')

FIELD=re.compile(rb'''(?i)(?<![A-Za-z0-9_])(?P<key>appId|app_id|appName|app_name|nickName|navigationBarTitleText|appVersion|app_version|versionName|versionCode|version|sdkVersion|SDKVersion|libVersion|developVersion|envVersion|developer|developerName|author|company|corpName|buildVersion|buildTime|extAppid|provider)["']?\s*[:=]\s*(?P<value>"(?:[^"\\\r\n]|\\.){0,500}"|'(?:[^'\\\r\n]|\\.){0,500}'|[0-9]+(?:\.[0-9]+){0,4})''')

DOMAIN=re.compile(rb'''(?i)https?://(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,63})(?::[0-9]{1,5})?''')

CONFIG_NAMES={'app.json','app-config.json','game.json','project.config.json','project.private.config.json','ext.json','plugin.json','package.json','manifest.json'}

HOST_NAMES={'app.json','app-config.json','game.json','project.config.json'}

def stamp():return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())

def safe_open(path):
 if os.path.realpath(path)!=path:raise ValueError('symlink ancestor or leaf rejected')
 fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
 s=os.fstat(fd)
 if not stat.S_ISREG(s.st_mode):os.close(fd);raise ValueError('not regular')
 return os.fdopen(fd,'rb'),s

def index_package(b):
 if len(b)<18 or b[0]!=190 or b[13]!=237:raise ValueError('unrecognized_header')
 unknown,index_len,body_len,count=struct.unpack('>IIII',b[1:13]+b[14:18])
 if count>MAX_ENTRIES:raise ValueError('entry_count_resource_limit')
 end=14+index_len
 if end>len(b) or end<18:raise ValueError('index_length_out_of_bounds')
 o=18;entries=[];names=set();warnings=[]
 for i in range(count):
  if o+4>end:raise ValueError('truncated_index_name_length')
  nl=struct.unpack_from('>I',b,o)[0];o+=4
  if nl>16384 or o+nl+8>end:raise ValueError('unsafe_or_truncated_index_name')
  raw=b[o:o+nl];name=raw.decode('utf8','strict');o+=nl;pos,size=struct.unpack_from('>II',b,o);o+=8
  norm=name[1:] if name.startswith('/') else name
  if '\x00' in name or '\\' in name or name.startswith('//') or '..' in PurePosixPath(norm).parts or re.match('^[A-Za-z]:',norm):raise ValueError('unsafe_member_path')
  if pos<end or pos+size>len(b):raise ValueError('entry_out_of_bounds')
  if name in names:warnings.append('duplicate_member_name')
  names.add(name);entries.append((name,pos,size))
 if o!=end:warnings.append('unused_index_bytes:'+str(end-o))
 if end+body_len!=len(b):warnings.append('header_total_size_mismatch')
 return entries,{'entry_count':count,'index_bytes':index_len,'body_bytes_declared':body_len,'header_unknown':unknown,'warnings':sorted(set(warnings))}

def metadata(b,entries):
 evidence=[];counts=collections.Counter();host=set();plugins=set();fields=collections.Counter();json_errors=[];configs=[];scanned_bytes=0
 member_sizes={(n,p):s for n,p,s in entries}
 def add(kind,name,pos,local,value,**extra):
  counts[kind]+=1
  if len(evidence)>=MAX_EVIDENCE:return
  if isinstance(value,bytes):value=value.decode('utf8','replace')
  snippet=b[max(pos,pos+local-100):min(pos+member_sizes[(name,pos)],pos+local+200)].decode('utf8','replace')
  evidence.append(dict(kind=kind,member=name,member_offset=local,package_offset=pos+local,value=value,snippet=snippet,**extra))
 for name,pos,size in entries:
  raw=b[pos:pos+size];scanned_bytes+=size;norm=name.lstrip('/');base=norm.rsplit('/',1)[-1]
  # Content identifiers are references unless an explicit trusted-scope manifest declares host identity.
  for m in WX.finditer(raw):
   value=m.group().decode('ascii');add('appid_reference',name,pos,m.start(),value,identity_role='reference_or_unresolved_not_host')
  for m in DOMAIN.finditer(raw):add('domain',name,pos,m.start('host'),m.group('host').lower())
  for m in FIELD.finditer(raw):
   key=m.group('key').decode('ascii');val=m.group('value');low=key.lower();fields[low]+=1
   kind='sdk_or_library_version' if low in ('sdkversion','libversion') else 'build_or_environment' if low in ('buildversion','buildtime','envversion','developversion') else 'declared_app_version' if low in ('appversion','app_version','versioncode','versionname') else 'generic_version_not_release' if low=='version' else 'name_or_title' if low in ('appname','app_name','nickname','navigationbartitletext') else 'developer' if low in ('developer','developername','author','company','corpname') else 'identity_assignment_candidate'
   add(kind,name,pos,m.start('value'),val,field=key,identity_role='unresolved_not_host' if kind=='identity_assignment_candidate' else 'not_identity_proof',release_certified=False)
  for m in re.finditer(rb'__plugin__/(wx[0-9a-fA-F]{16})',name.encode('utf8')):plugins.add(m.group(1).decode('ascii'))
  if base not in CONFIG_NAMES:continue
  config={'member':name,'offset':pos,'size':size,'sha256':hashlib.sha256(raw).hexdigest()};configs.append(config)
  if size>MAX_JSON:config['status']='json_resource_limit';continue
  try:obj=json.loads(raw.decode('utf-8-sig'));config['status']='json_decoded'
  except Exception as e:config['status']='not_json_or_malformed';json_errors.append({'member':name,'error':str(e)[:200]});continue
  # Only root application/project manifests can declare host. extAppid/provider/plugin IDs never count.
  if norm in HOST_NAMES and isinstance(obj,dict):
   for key in ('appid','appId','appID','app_id'):
    value=obj.get(key)
    if isinstance(value,str) and re.fullmatch('wx[0-9a-fA-F]{16}',value):
     host.add(value);pat=re.compile(rb'"'+key.encode()+rb'"\s*:\s*"'+value.encode()+rb'"');match=pat.search(raw)
     add('host_manifest_declaration',name,pos,match.start() if match else 0,value,json_path='/'+key,identity_role='explicit_root_manifest_host_declaration',independent_of_filename_or_key=True)
  # Structured plugin providers, including plugin dependencies, are references only.
  stack=[('',obj)];visited=0
  while stack:
   jpath,v=stack.pop();visited+=1
   if visited>200000:config['tree_limit']=True;break
   if isinstance(v,dict):
    for k,x in v.items():
     kp=jpath+'/'+str(k)
     if k in ('provider','pluginId','pluginAppid') and isinstance(x,str) and re.fullmatch('wx[0-9a-fA-F]{16}',x):plugins.add(x)
     if isinstance(x,(list,dict)):stack.append((kp,x))
   elif isinstance(v,list):stack.extend((jpath+'/'+str(i),x) for i,x in enumerate(v) if isinstance(x,(dict,list)))
 return {'host_appids':sorted(host),'plugin_ids':sorted(plugins),'evidence_counts':dict(counts),'fields_counts':dict(fields),'evidence_records':len(evidence),'evidence_truncated':sum(counts.values())>len(evidence),'configs':configs,'json_errors':json_errors,'member_bytes_scanned_with_overlaps':scanned_bytes},evidence

def parse_one(row):
 with open(__file__,'rb') as parser_source: parser_sha256=hashlib.sha256(parser_source.read()).hexdigest()
 sha,size,path=row;r={'sha256':sha,'size':size,'source_path':path,'parser_version':VERSION,'parser_sha256':parser_sha256,'utc':stamp()}
 try:
  if size>MAX_PACKAGE:r.update(status='resource_limit_package_size',host_appids=[]);return r,[]
  f,s=safe_open(path)
  with f:b=f.read(MAX_PACKAGE+1)
  if len(b)!=size or hashlib.sha256(b).hexdigest()!=sha:raise ValueError('source_changed_after_inventory')
  r['header_hex']=b[:18].hex()
  if b.startswith(b'V1MMWX'):
   r.update(status='encrypted_V1MMWX_unsupported_no_local_authorized_decryptor',host_appids=[],candidate_key_is_not_independent_proof=True);return r,[]
  if b[:1]!=b'\xbe' or b[13:14]!=b'\xed':
   r.update(status='unsupported_zip' if b.startswith(b'PK\x03\x04') else 'unsupported_or_malformed_header',host_appids=[]);return r,[]
  entries,info=index_package(b);r.update(info)
  md,ev=metadata(b,entries);r.update(md)
  r['role']='plugin' if any('__plugin__/' in name or name.lstrip('/')=='plugin.json' for name,_,_ in entries) else 'host_main_candidate' if any(name.lstrip('/') in HOST_NAMES for name,_,_ in entries) else 'subpackage_or_other'
  r['status']='plaintext_parsed'
  r['members']=[{'member':name,'offset':pos,'size':sz} for name,pos,sz in entries]
  return r,ev
 except Exception as e:r.update(status='malformed_or_read_error',error=repr(e),host_appids=[]);return r,[]
