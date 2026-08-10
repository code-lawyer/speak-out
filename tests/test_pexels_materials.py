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
