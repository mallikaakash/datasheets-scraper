import asyncio, httpx, time

async def test_concurrency(n, max_concurrent=200):
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def limited_get(client, url):
        async with semaphore:
            return await client.get(url, timeout=50.0)
    
    async with httpx.AsyncClient() as c:
        start = time.time()
        await asyncio.gather(*[limited_get(c, "https://www.datasheets.com/") for _ in range(n)])
        print(f"{n} requests ({max_concurrent} concurrent) took {time.time()-start:.1f}s")

asyncio.run(test_concurrency(1000))