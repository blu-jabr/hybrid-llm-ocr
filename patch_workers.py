import re

p = open("ocr_pipeline.py").read()

# 1. Add ThreadPoolExecutor + time/random imports if missing
if "from concurrent.futures import ThreadPoolExecutor" not in p:
    p = p.replace(
        "import argparse",
        "import argparse\nimport random\nfrom concurrent.futures import ThreadPoolExecutor, as_completed",
        1,
    )

# 2. Add retry loop around cloud HTTP post
old_post = "resp = requests.post(url, headers=headers, json=payload, timeout=180)"
new_post = """for _attempt in range(5):
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        if resp.status_code in (429, 502, 503, 504) and _attempt < 4:
            time.sleep((2 ** _attempt) + random.uniform(0.2, 1.0))
            continue
        break"""
assert old_post in p, "requests.post line not found"
p = p.replace(old_post, new_post, 1)

# 3. Add --workers CLI flag
old_flag = 'parser.add_argument("--limit", type=int, default=0'
new_flag = 'parser.add_argument("--workers", type=int, default=1, help="Concurrent API workers")\n    parser.add_argument("--limit", type=int, default=0'
assert old_flag in p, "--limit flag not found"
p = p.replace(old_flag, new_flag, 1)

# 4. Run process_page across ThreadPoolExecutor when --workers > 1
old_loop = """    for row in pending:
        process_page(row, args, db_path, raw_dir)"""
new_loop = """    if args.workers <= 1:
        for row in pending:
            process_page(row, args, db_path, raw_dir)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(process_page, row, args, db_path, raw_dir) for row in pending]
            for fut in as_completed(futs):
                fut.result()"""
assert old_loop in p, "pending loop not found"
p = p.replace(old_loop, new_loop, 1)

open("ocr_pipeline.py", "w").write(p)
print("Workers & 429 retry patched OK")
