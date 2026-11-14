"""存档：把当前工作区（多个文件夹 + 全部哈希记录 + 设置）存成 JSON。

用途：
* 扫描结果保留下来，随时载入继续处理，不用重扫；
* 换机器/备份：把 .json 拷到别处，导入即可（缩略图会按原路径重新生成，
  所以目标机器上要能看到同样的照片路径）。
"""

from __future__ import annotations

import json
import os
import time
import uuid

VERSION = 1
APP_TAG = "photo-dedupe"


def ensure(adir: str) -> str:
    """存档目录（可以放在软件目录、图片文件夹或任意位置）。"""
    d = os.path.abspath(adir)
    os.makedirs(d, exist_ok=True)
    return d


SCHEMA = 1          # 存档格式版本；改动结构时 +1，老文件仍按缺失字段兼容读取


def scope_dir(adir: str, scope: str = "image") -> str:
    """图片存档放 archives 根目录，媒体存档独立放 archives/media，互不影响。"""
    d = ensure(adir)
    if scope in ("video", "audio", "novel"):
        d = os.path.join(d, scope)
        os.makedirs(d, exist_ok=True)
    return d


def _index_path(d: str) -> str:
    return os.path.join(d, "index.json")


def _read_index(d: str) -> list[dict]:
    try:
        with open(_index_path(d), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_index(d: str, items: list[dict]):
    tmp = _index_path(d) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _index_path(d))


def _media_counts(payload: dict) -> dict:
    counts = {"video": 0, "audio": 0, "novel": 0}
    for it in ((payload.get("media") or {}).get("items") or []):
        k = it.get("kind")
        if k in counts:
            counts[k] += 1
    return counts


def _meta(payload: dict, size: int) -> dict:
    return {
        "id": payload["id"],
        "name": payload.get("name") or "未命名存档",
        "created": payload.get("created") or time.time(),
        "images": len(payload.get("images") or []),
        "roots": len(payload.get("roots") or []),
        "media": _media_counts(payload),
        "scope": payload.get("scope") or "image",
        "size": size,
        "version": payload.get("version", VERSION),
    }


def _payload_ok(p: dict) -> bool:
    if not isinstance(p, dict) or p.get("app") != APP_TAG:
        return False
    if p.get("scope") == "media":
        return isinstance(p.get("media"), dict)
    return isinstance(p.get("images"), list) and isinstance(p.get("roots"), list)


def list_all(adir: str, scope: str = "image") -> list[dict]:
    d = scope_dir(adir, scope)
    items = _read_index(d)
    # 索引丢失时按目录内容重建
    if not items:
        rebuilt = []
        for fn in os.listdir(d):
            if not fn.endswith(".json") or fn == "index.json":
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    p = json.load(f)
                if _payload_ok(p):
                    rebuilt.append(_meta(p, os.path.getsize(os.path.join(d, fn))))
            except Exception:
                continue
        if rebuilt:
            rebuilt.sort(key=lambda x: x["created"], reverse=True)
            _write_index(d, rebuilt)
        items = rebuilt
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return items


def save(store, adir: str, name: str, settings: dict, aid: str | None = None,
         media=None, scope: str = "image") -> dict:
    """保存存档。给了 aid 就覆盖那一条（用于"更新存档"）。"""
    d = scope_dir(adir, scope)
    aid = aid or f"a{int(time.time())}_{uuid.uuid4().hex[:6]}"
    payload = {
        "app": APP_TAG,
        "version": VERSION,
        "schema": SCHEMA,
        "scope": scope,
        "id": aid,
        "name": (name or "").strip() or time.strftime("存档 %Y-%m-%d %H:%M"),
        "created": time.time(),
        "settings": settings or {},
    }
    if scope == "media":
        payload["roots"] = []
        payload["images"] = []
        try:
            payload["media"] = media.dump_all() if media is not None else {"items": [], "roots": []}
        except Exception:
            payload["media"] = {"items": [], "roots": []}
    else:
        payload["roots"] = store.dump_roots()
        payload["images"] = store.dump_images()
    path = os.path.join(d, aid + ".json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        # 半截文件留着既占地方又像"存档坏了"，清掉再抛
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    items = [i for i in _read_index(d) if i.get("id") != aid]
    items.insert(0, _meta(payload, os.path.getsize(path)))
    _write_index(d, items)
    return items[0]


def read(adir: str, aid: str, scope: str = "image") -> dict | None:
    d = scope_dir(adir, scope)
    if not aid or "/" in aid or "\\" in aid or ".." in aid:
        return None
    path = os.path.join(d, aid + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            p = json.load(f)
        return p if _payload_ok(p) else None
    except Exception:
        return None


def file_path(adir: str, aid: str, scope: str = "image") -> str | None:
    d = scope_dir(adir, scope)
    if not aid or "/" in aid or "\\" in aid or ".." in aid:
        return None
    path = os.path.join(d, aid + ".json")
    return path if os.path.exists(path) else None


def remove(adir: str, aid: str, scope: str = "image") -> bool:
    d = scope_dir(adir, scope)
    path = file_path(adir, aid)
    if not path:
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    _write_index(d, [i for i in _read_index(d) if i.get("id") != aid])
    return True


def save_upload(adir: str, payload: dict, scope: str = "image") -> dict:
    """导入外部存档文件（内容已在内存里）。"""
    if not _payload_ok(payload):
        raise ValueError("不是有效的存档文件")
    d = scope_dir(adir, scope)
    aid = payload.get("id") or f"a{int(time.time())}_{uuid.uuid4().hex[:6]}"
    # 避免覆盖同 id 的既有存档
    if os.path.exists(os.path.join(d, str(aid) + ".json")):
        aid = f"a{int(time.time())}_{uuid.uuid4().hex[:6]}"
    payload["id"] = aid
    payload.setdefault("created", time.time())
    payload["name"] = payload.get("name") or time.strftime("导入存档 %Y-%m-%d %H:%M")
    path = os.path.join(d, str(aid) + ".json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    items = [i for i in _read_index(d) if i.get("id") != aid]
    items.insert(0, _meta(payload, os.path.getsize(path)))
    _write_index(d, items)
    return _meta(payload, os.path.getsize(path))

