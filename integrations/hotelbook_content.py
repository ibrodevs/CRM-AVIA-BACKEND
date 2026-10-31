"""Read-only HBPRO hotel content; dictionaries stay scoped to the supplier cache."""

from math import isfinite
from urllib.parse import urlsplit

from django.utils.html import strip_tags


def hotel_coordinates(hotel):
    """Supplier hotel coordinates only; a city centre is not a hotel location."""
    for source in (hotel.get("details") or {}, hotel):
        lat, lon = source.get("latitude"), source.get("longitude")
        if lat is None or lon is None or isinstance(lat, bool) or isinstance(lon, bool):
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if isfinite(lat) and isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180:
            return {"latitude": lat, "longitude": lon}
    return {}


def public_image(url):
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        return url if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username else ""
    except ValueError:
        return ""


def hotel_content(adapter, ctx, snapshot):
    hb = snapshot.get("hotelbook") or {}
    _, config = adapter._config(ctx)
    hotel = adapter._hotel(ctx, config, hb["hotel_id"])
    rooms = adapter._dictionary(ctx, config, "hotel/rooms", {"hotelId": hb["hotel_id"]})
    details = hotel.get("details") or {}
    amenities = {
        r["id"]: r.get("description") or r.get("name", "")
        for r in hotel.get("roomAmenities", [])
        if "id" in r
    }

    def photos(rows, room=False):
        result = []
        for row in rows or []:
            variants = row.get("variants") or {} if room else {}
            url = public_image(variants.get("primary") if room else row.get("photoUrl"))
            if url:
                result.append(
                    {
                        "url": url,
                        "thumbnail": public_image(
                            variants.get("preview") if room else row.get("thumbnailUrl")
                        )
                        or url,
                        "description": strip_tags(str(row.get("description") or "")),
                    }
                )
        return result

    return {
        "source": "hotelbook",
        "hotel": {
            "id": hb["hotel_id"],
            **hotel_coordinates(hotel),
            "name": hotel.get("name") or snapshot.get("itinerary", {}).get("property_name", ""),
            "address": details.get("address", ""),
            "description": strip_tags(details.get("description") or ""),
            "photos": photos(sorted(hotel.get("photos") or [], key=lambda p: not p.get("isMain"))),
        },
        "rooms": [
            {
                "id": str(room["id"]),
                "name": room.get("name", ""),
                "description": strip_tags(room.get("description") or ""),
                "amenities": [amenities[a] for a in room.get("amenityIds", []) if amenities.get(a)],
                "photos": photos(room.get("images"), room=True),
            }
            for room in rooms
            if room.get("hotelId") == hb["hotel_id"] and not room.get("deletedAt") and room.get("id")
        ],
        "selected_room_ids": [
            str(r["roomInfoId"]) for r in hb.get("offer", {}).get("rooms", []) if r.get("roomInfoId")
        ],
    }
