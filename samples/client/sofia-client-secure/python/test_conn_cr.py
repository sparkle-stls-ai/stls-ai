import httpx, asyncio, os
print(os.getenv("HTTPS_PROXY"), os.getenv("NO_PROXY"))
async def t():
    async with httpx.AsyncClient() as c:  # trust_env=True di default
        r = await c.get("https://sofia-admin-508683109767.europe-west8.run.app/health")
        print(r.status_code)
asyncio.run(t())