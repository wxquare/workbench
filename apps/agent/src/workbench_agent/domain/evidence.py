"""Bounded, redacted evidence payloads for durable source inspection."""
import json
import re

_SECRET_KEY=re.compile(r'(secret|token|password|passwd|authorization|cookie|session[_-]?key|api[_-]?key)',re.I)
_SECRET_VALUE=re.compile(r'(?i)(bearer\s+|sk-)[a-z0-9._~+/=-]+')

def _clean(value):
    if isinstance(value,dict):
        return {str(k):'[REDACTED]' if _SECRET_KEY.search(str(k)) else _clean(v) for k,v in value.items()}
    if isinstance(value,list): return [_clean(v) for v in value]
    if isinstance(value,str): return _SECRET_VALUE.sub('[REDACTED]',value)
    return value

def safe_evidence_payload(value,max_bytes):
    clean=_clean(value)
    encoded=json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode('utf-8'))<=max_bytes: return encoded,False
    low,high=0,len(encoded)
    while low<high:
        mid=(low+high+1)//2
        candidate=json.dumps({'excerpt':encoded[:mid],'truncated':True},ensure_ascii=False,separators=(',',':'))
        if len(candidate.encode('utf-8'))<=max_bytes: low=mid
        else: high=mid-1
    result=json.dumps({'excerpt':encoded[:low],'truncated':True},ensure_ascii=False,separators=(',',':'))
    if len(result.encode('utf-8'))>max_bytes: raise ValueError('evidence limit too small')
    return result,True
