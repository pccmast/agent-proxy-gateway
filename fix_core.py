"""Fix proxy/core.py — remove build_request and use inline stream() call."""

path = "src/gateway/proxy/core.py"
content = open(path, encoding="utf-8").read()

# Pattern: build_request + stream(request)
old = """        upstream_req = client.build_request("POST", url, json=body, headers=headers)

        async def generate():
            async with client.stream(upstream_req) as response:"""

new = """        async def generate():
            async with client.stream("POST", url, json=body, headers=headers) as response:"""

if old in content:
    content = content.replace(old, new)
    open(path, "w", encoding="utf-8").write(content)
    print("Fixed: replaced build_request+stream")
else:
    # Try line-by-line search
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "build_request" in line or "upstream_req" in line:
            print(f"L{i + 1}: {line}")
    print("Pattern not found in file")
