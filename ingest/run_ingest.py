"""Ingest destination guides: extract -> chunk -> enrich w/ metadata -> embed -> index.

Each guide is a markdown file named after the city (e.g. Lisbon.md). The city is
the document id; lightweight metadata (city, country, region) is attached to each
chunk so retrieval can filter by destination. The shared knowledge base is tagged
shared=true; per-user uploaded docs (added later) are tagged with a user_id.
"""
from pathlib import Path
from app.config import get_settings
from app.embeddings import embed
from app.stores import vector_opensearch as vs
from ingest.extract import extract
from ingest.chunk import chunk_text

_s = get_settings()
DATA = Path(_s.travel_guides_dir)

# Optional per-city enrichment (country, region) for filterable retrieval.
# NOT a gate: any guide file in the folder is ingested. A city missing from
# this map simply gets blank country/region. To add a city: drop its .md file
# in the guides folder. (Optionally add a line here for country/region filtering.)
_CITY_META = {
    "Lisbon": ("Portugal", "Europe"), "Tokyo": ("Japan", "Asia"),
    "Barcelona": ("Spain", "Europe"), "Bangkok": ("Thailand", "Asia"),
    "Reykjavik": ("Iceland", "Europe"), "Mexico_City": ("Mexico", "North America"),
    "Cape_Town": ("South Africa", "Africa"), "Queenstown": ("New Zealand", "Oceania"),
    "Marrakech": ("Morocco", "Africa"), "Vancouver": ("Canada", "North America"),
    "Amsterdam": ("Netherlands", "Europe"), "Copenhagen": ("Denmark", "Europe"),
    "Vienna": ("Austria", "Europe"), "Porto": ("Portugal", "Europe"),
    "Rome": ("Italy", "Europe"), "Prague": ("Czech Republic", "Europe"),
    "Singapore": ("Singapore", "Asia"), "Kyoto": ("Japan", "Asia"),
    "Seoul": ("South Korea", "Asia"), "Delhi": ("India", "Asia"),
    "Dubai": ("United Arab Emirates", "Middle East"),
    "New_York": ("United States", "North America"),
    "San_Francisco": ("United States", "North America"),
    "Buenos_Aires": ("Argentina", "South America"),
    "Sydney": ("Australia", "Oceania"), "Nairobi": ("Kenya", "Africa"),
}


def main():
    docs = [p for p in DATA.iterdir()
            if p.suffix.lower() in (".md", ".txt", ".pdf")]
    all_chunks: list[dict] = []
    for doc in docs:
        city = doc.stem
        country, region = _CITY_META.get(city, ("", ""))
        for page, text in extract(doc):
            for piece in chunk_text(text):
                all_chunks.append({
                    "text": piece,
                    "city": city.replace("_", " "),
                    "country": country, "region": region,
                    "section": "guide", "page": page,
                    "shared": True,          # shared KB (vs per-user uploads)
                })
    print(f"embedding {len(all_chunks)} chunks...")
    vectors = embed([c["text"] for c in all_chunks])
    for c, v in zip(all_chunks, vectors):
        c["embedding"] = v
    vs.index_chunks(all_chunks)
    print(f"indexed {len(all_chunks)} chunks into '{_s.opensearch_index}'")


if __name__ == "__main__":
    main()
