import json, urllib.request
URL = "http://127.0.0.1:8799"
def post(p, b):
    req = urllib.request.Request(URL+p, data=json.dumps(b).encode(), headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
def get(p):
    return json.loads(urllib.request.urlopen(URL+p, timeout=30).read().decode())

print("hashcat:", {k: v for k, v in get("/api/brute/hashcat").items() if k in ("available", "version", "speed", "devices")})

cases = {
  "只密码本 3 条": {"passwords": ["a", "b", "c"], "brute": {"auto": False, "rules": {}}},
  "密码本+变形": {"passwords": ["abc", "love"], "brute": {"auto": False,
                 "rules": {"case": True, "digits": 2, "leet": True}}},
  "数字 6 位": {"passwords": [], "brute": {"auto": True, "elements": [
      {"type": "num", "from": 0, "to": 999999, "width": 0}]}},
  "数字 6 位 + 前缀后缀": {"passwords": [], "brute": {"auto": True, "elements": [
      {"type": "literal", "value": "abc"},
      {"type": "num", "from": 0, "to": 999999, "width": 0},
      {"type": "literal", "value": "!"}]}},
  "小写 1~5 位": {"passwords": [], "brute": {"auto": True, "elements": [
      {"type": "charset", "charset": "lower", "len_min": 1, "len_max": 5}]}},
  "混合 6 位": {"passwords": [], "brute": {"auto": True, "elements": [
      {"type": "charset", "charset": "mix", "len_min": 6, "len_max": 6}]}},
}
for name, body in cases.items():
    r = post("/api/brute/plan", body)
    print(f"{name:<20} 密码本 {r['dict_count']:>3} → {r['dict_variants']:>6}  穷举 {r['brute_count']:>14,} "
          f"共 {r['total']:>14,} 预计 GPU {r['eta_gpu']:>9}s / CPU {r['eta_cpu']:>10}s  [{r['label']}]")
