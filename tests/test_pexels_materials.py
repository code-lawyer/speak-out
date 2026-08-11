import json

import httpx

from agent_content_pipeline.video.materials import PexelsMaterialSource


def test_pexels_source_downloads_landscape_renditions_and_keeps_attribution(tmp_path):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.pexels.com":
            return httpx.Response(
                200,
                json={
                    "videos": [
                        {
                            "id": 42,
                            "duration": 12,
                            "url": "https://www.pexels.com/video/42/",
                            "user": {"name": "Example Creator"},
                            "video_files": [
                                {
                                    "width": 1080,
                                    "height": 1920,
                                    "link": "https://cdn.example/portrait.mp4",
                                },
                                {
                                    "width": 1920,
                                    "height": 1080,
                                    "link": "https://cdn.example/landscape.mp4",
                                },
                            ],
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(200, content=b"landscape-video", request=request)

    source = PexelsMaterialSource(
        api_key="pexels-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    downloads = source.acquire(
        terms=["technology"],
        destination=tmp_path / "materials",
        max_files=1,
    )

    assert downloads[0].path.read_bytes() == b"landscape-video"
    assert downloads[0].width == 1920
    assert downloads[0].height == 1080
    assert requests[0].headers["Authorization"] == "pexels-secret"
    assert requests[0].url.params["orientation"] == "landscape"
    assert str(requests[1].url) == "https://cdn.example/landscape.mp4"
    metadata = json.loads((tmp_path / "materials" / "materials.json").read_text("utf-8"))
    assert metadata[0]["creator"] == "Example Creator"
    assert "pexels-secret" not in json.dumps(metadata)


def test_pexels_source_round_robins_across_search_terms(tmp_path):
    def video(asset_id: int) -> dict:
        return {
            "id": asset_id,
            "duration": 12,
            "url": f"https://www.pexels.com/video/{asset_id}/",
            "user": {"name": f"Creator {asset_id}"},
            "video_files": [
                {
                    "width": 1920,
                    "height": 1080,
                    "link": f"https://cdn.example/{asset_id}.mp4",
                }
            ],
        }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.pexels.com":
            base = 100 if request.url.params["query"] == "law" else 200
            return httpx.Response(
                200,
                json={"videos": [video(base + index) for index in range(3)]},
                request=request,
            )
        return httpx.Response(200, content=b"video", request=request)

    source = PexelsMaterialSource(
        api_key="pexels-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    downloads = source.acquire(
        terms=["law", "technology"],
        destination=tmp_path / "materials",
        max_files=4,
    )

    assert [item.search_term for item in downloads] == [
        "law",
        "technology",
        "law",
        "technology",
    ]
