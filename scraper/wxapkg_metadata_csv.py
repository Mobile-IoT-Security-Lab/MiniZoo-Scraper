"""Synchronous acquisition-time metadata export; literals are not certified releases."""
import csv
import hashlib
import json
from pathlib import Path
import static_wxapkg_reader as reader
from opened_app_identity import SCHEMA, METADATA_FIELDS, load_verified_receipt

KINDS = {
    'extracted_names_json': 'name_or_title',
    'extracted_developers_json': 'developer',
    'extracted_app_versions_json': 'declared_app_version',
    'extracted_sdk_versions_json': 'sdk_or_library_version',
    'extracted_generic_versions_json': 'generic_version_not_release',
    'extracted_domains_json': 'domain',
    'extracted_candidate_appids_json': 'identity_assignment_candidate',
    'extracted_reference_appids_json': 'appid_reference',
}
EXTRA_FIELDS = [
    'queried_appid','queried_name','queried_app_version','queried_developer',
    'query_record_json','source_run','package_local_path','remote_source',
    'package_sha256','extraction_status','package_role','package_attribution',
    'identity_status','certified_release','extraction_confidence',
    'extracted_host_appids_json','extracted_plugin_ids_json',*KINDS,
    'all_extracted_fields_json','extraction_evidence_path','extraction_summary_path',
    'parser_version','parser_sha256','evidence_truncated',
]


def literal(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        # Retain single-quoted JS literals exactly: never evaluate JS/Python.
        return value


def unique(values):
    return [json.loads(v) for v in sorted({json.dumps(v, ensure_ascii=False, sort_keys=True) for v in values})]


def queried(record, field):
    value = record.get('queried_' + field, '')
    return value if record.get('collection_schema') == SCHEMA else value or record.get(field, '')


def append_collected_package_metadata(csv_path, local_files, query_record, *, source_run, remote_sources=(), base_fields=()):
    """One row per package SHA/provenance, never substitute CSV query for content."""
    csv_path = Path(csv_path)
    receipt = None
    if query_record.get('package_attribution') == 'runtime_verified':
        receipt, _ = load_verified_receipt(query_record, csv_path.parent)
    fields = list(dict.fromkeys([*base_fields, *EXTRA_FIELDS]))
    if not base_fields:
        fields = list(dict.fromkeys(['appid','name','developer','app_version','sha256',*fields]))
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists()
    seen = set()
    if exists:
        with csv_path.open(newline='', encoding='utf-8-sig') as source:
            existing = csv.DictReader(source)
            if existing.fieldnames != fields:
                raise ValueError('package_metadata_schema_mismatch_use_new_output')
            seen = {(r['package_sha256'],r['source_run'],r['package_local_path'],r['remote_source'],r['query_record_json']) for r in existing}
    written = 0
    with csv_path.open('a', newline='', encoding='utf-8') as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for index, item in enumerate(local_files):
            path = Path(item).absolute()
            with reader.safe_open(str(path))[0] as source:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024*1024), b''):
                    hasher.update(chunk)
                digest = hasher.hexdigest()
            expected = query_record.get('sha256','').split(';')
            if index < len(expected) and expected[index] and expected[index] != digest:
                raise ValueError('package_sha256_mismatch')
            remote = remote_sources[index] if index < len(remote_sources) else ''
            query_json = json.dumps(query_record, ensure_ascii=False, sort_keys=True)
            key = (digest,source_run,str(path),remote,query_json)
            if key in seen:
                continue
            directory = csv_path.parent/'package_metadata_evidence'/digest
            directory.mkdir(parents=True, exist_ok=True)
            evidence_path = directory/'evidence.jsonl'
            summary_path = directory/'summary.json'
            if summary_path.exists() and evidence_path.exists():
                result = json.loads(summary_path.read_text(encoding='utf-8'))
                evidence = [json.loads(line) for line in evidence_path.read_text(encoding='utf-8').splitlines()]
            else:
                result, evidence = reader.parse_one((digest, path.stat().st_size, str(path)))
                for entry in evidence:
                    entry['source_sha256'] = digest
                with evidence_path.open('x',encoding='utf-8') as stream:
                    stream.write(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in evidence))
                with summary_path.open('x',encoding='utf-8') as stream:
                    stream.write(json.dumps(result, ensure_ascii=False, indent=2))
            row = {field: query_record.get(field, '') for field in base_fields}
            for field, value in {'local_files':str(path),'remote_sources':remote,'package_size_bytes':path.stat().st_size,'md5':'','username':'','metadata_verified_at':''}.items():
                if field in fields:
                    row[field] = value
            row.update(appid='',name='',developer='',app_version='',sha256=digest,
                       queried_appid=queried(query_record,'appid'),
                       queried_name=queried(query_record,'name'),
                       queried_app_version=queried(query_record,'app_version'),
                       queried_developer=queried(query_record,'developer'),
                       query_record_json=json.dumps(query_record, ensure_ascii=False, sort_keys=True),
                       source_run=source_run,package_local_path=str(path),
                       remote_source=remote_sources[index] if index < len(remote_sources) else '',
                       package_sha256=digest,extraction_status=result['status'],package_role=result.get('role','unknown'),
                       package_attribution='UNASSIGNED',identity_status='static_declaration_only_not_ownership_proof',
                       certified_release='false',extraction_confidence='static_literal_not_certified',
                       extraction_evidence_path=str(evidence_path),extraction_summary_path=str(summary_path),
                       parser_version=result['parser_version'],parser_sha256=result['parser_sha256'],
                       evidence_truncated=json.dumps(result.get('evidence_truncated',False)))
            # Extraction fields below remain byte-derived. Runtime metadata has
            # its own verified provenance and never borrows the requested name.
            for key in ('category', 'rating', 'url', 'description', 'icon_url', 'doc_id', 'source_query'):
                if key in fields:
                    row[key] = ''
            if receipt is not None:
                row.update({key: receipt['metadata'].get(key, '')
                            for key in METADATA_FIELDS if key in fields})
                row.update(package_attribution='runtime_verified', identity_status='runtime_verified')
                for key, value in {'md5':hashlib.md5(path.read_bytes()).hexdigest(),
                                   'metadata_verified_at':receipt['observed_at'],
                                   'csv_id':receipt['appid']}.items():
                    if key in fields:
                        row[key] = value
            for column,kind in KINDS.items():
                row[column] = json.dumps(unique([literal(e['value']) for e in evidence if e['kind']==kind]),ensure_ascii=False)
            row['extracted_host_appids_json'] = json.dumps(result.get('host_appids',[]))
            row['extracted_plugin_ids_json'] = json.dumps(result.get('plugin_ids',[]))
            grouped = {}
            for e in evidence:
                grouped.setdefault(e['kind'],{}).setdefault(e.get('field',''),[]).append(literal(e['value']))
            row['all_extracted_fields_json'] = json.dumps({k:{f:unique(v) for f,v in fields.items()} for k,fields in grouped.items()}, ensure_ascii=False,sort_keys=True)
            writer.writerow(row)
            destination.flush()
            seen.add(key)
            written += 1
    return written
