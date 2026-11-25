# -*- coding: utf-8 -*-
"""手工下载 HF 大文件（走 2080 代理，支持续传、带进度）。"""
import http.client, ssl, os, sys, time
PROXY = ("127.0.0.1", 2080)
def conn_for(host, timeout=60):
    c = http.client.HTTPSConnection(PROXY[0], PROXY[1], timeout=timeout, context=ssl.create_default_context())
    c.set_tunnel(host, 443)
    return c

def resolve(url):
    host = url.split("/")[2]
    path = "/" + url.split("/", 3)[3]
    for _ in range(6):
        c = conn_for(host)
        c.request("GET", path, headers={"User-Agent": "Mozilla/5.0"})
        r = c.getresponse()
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader("Location")
            c.close()
            if loc.startswith("http"):
                host = loc.split("/")[2]; path = "/" + loc.split("/", 3)[3]
            else:
                path = loc
            continue
        c.close()
        return host, path, r.status
    raise RuntimeError("too many redirects")

def download(url, dst, chunk=1 << 20):
    host, path, st = resolve(url)
    print("-> %s %s (%d)" % (host, path[:80], st), flush=True)
    done = os.path.getsize(dst) if os.path.exists(dst) else 0
    t0 = time.time(); last = 0.0
    while True:
        c = conn_for(host, timeout=120)
        hdr = {"User-Agent": "Mozilla/5.0"}
        if done:
            hdr["Range"] = "bytes=%d-" % done
        c.request("GET", path, headers=hdr)
        r = c.getresponse()
        total = None
        cr = r.getheader("Content-Range")
        if cr and "/" in cr:
            total = int(cr.split("/")[-1])
        elif r.getheader("Content-Length"):
            total = done + int(r.getheader("Content-Length"))
        if r.status not in (200, 206):
            raise RuntimeError("HTTP %d %s" % (r.status, (r.read() or b"")[:200]))
        mode = "ab" if r.status == 206 else "wb"
        if mode == "wb":
            done = 0
        with open(dst, mode) as fh:
            while True:
                try:
                    b = r.read(chunk)
                except Exception as e:
                    print("\n读取中断：%r，续传" % (repr(e)[:80]), flush=True)
                    break
                if not b:
                    break
                fh.write(b); done += len(b)
                now = time.time()
                if now - last > 3:
                    last = now
                    sp = done / max(1e-6, now - t0) / 1048576.0
                    print("\r%.1f / %s MB  %.1f MB/s" % (done / 1048576.0,
                          ("%.1f" % (total / 1048576.0)) if total else "?", sp), end="", flush=True)
        c.close()
        if total and done >= total:
            print("\n完成 %.1f MB" % (done / 1048576.0), flush=True)
            return True
        if not total:
            print("\n无总长，按结束处理 %.1f MB" % (done / 1048576.0), flush=True)
            return True
        print("\n未完成 %d/%d，继续" % (done, total), flush=True)
        time.sleep(0.5)

if __name__ == "__main__":
    download(sys.argv[1], sys.argv[2])
