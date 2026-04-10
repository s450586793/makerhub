from dataclasses import dataclass
from typing import List


@dataclass
class CrawlerFeatureMap:
    extract_next_data: bool = True
    fetch_design_from_api: bool = True
    collect_comments: bool = True
    parse_summary: bool = True
    fetch_instance_3mf: bool = True
    extract_instances: bool = True
    archive_model: bool = True


class LegacyCrawlerBridge:
    """
    新项目里的爬虫服务边界。

    下一步会把旧项目中这些能力迁入这里：
    - extract_next_data
    - fetch_design_from_api
    - collect_comments
    - parse_summary
    - fetch_instance_3mf
    - extract_instances
    - archive_model
    """

    def __init__(self) -> None:
        self.features = CrawlerFeatureMap()

    def supported_input_types(self) -> List[str]:
        return [
            "single_model",
            "author_upload",
            "collection_models",
        ]

    def archive(self, url: str) -> dict:
        return {
            "accepted": True,
            "url": url,
            "mode": "queued",
            "message": "新项目骨架已接管请求，下一步接入旧爬虫核心。",
        }

