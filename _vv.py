import asyncio, sys, os, io, pathlib
sys.path.insert(0, os.getcwd())
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from app.clients.llm import get_llm
from app.services.vision import to_data_uri
from app.agent.prompts import VLM_SYSTEM
async def main():
    llm = get_llm()
    print("text model  :", getattr(llm.text, "model", "?"))
    print("vision model:", getattr(llm.vision, "model", "?"))
    print("backup      :", getattr(llm.backup, "model", None))
    img = next(pathlib.Path("data/uploads").glob("*.jpg"))
    data, usage = await llm.vision_json([
        {"role": "system", "content": VLM_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": "Describe this photo."},
            {"type": "image_url", "image_url": {"url": to_data_uri(img.read_bytes(), "image/jpeg")}}]},
    ], default={})
    print("vision keys :", list(data.keys()), "| brand:", (data.get("detected") or {}).get("brand"))
    print("vision cost : $%.6f" % usage.cost_credits)
asyncio.run(main())
