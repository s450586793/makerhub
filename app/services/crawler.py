from typing import List

from app.services.archive_worker import ArchiveTaskManager


class CrawlerFeatureMap:
    def __init__(self) -> None:
        self.extract_next_data = True
        self.fetch_design_from_api = True
        self.collect_comments = True
        self.parse_summary = True
        self.fetch_instance_3mf = True
        self.extract_instances = True
        self.archive_model = True


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
        self.manager = ArchiveTaskManager()

    def supported_input_types(self) -> List[str]:
        return [
            "single_model",
            "author_upload",
            "collection_models",
        ]

    def archive(self, url: str) -> dict:
        return self.manager.submit(url)

    def preview_archive(self, url: str) -> dict:
        return self.manager.preview(url)

    def retry_missing_3mf(self, model_url: str, model_id: str = "", title: str = "", instance_id: str = "") -> dict:
        return self.manager.retry_missing_3mf(
            model_url=model_url,
            model_id=model_id,
            title=title,
            instance_id=instance_id,
        )

    def cancel_missing_3mf(self, model_url: str, model_id: str = "", title: str = "", instance_id: str = "") -> dict:
        return self.manager.cancel_missing_3mf(
            model_url=model_url,
            model_id=model_id,
            title=title,
            instance_id=instance_id,
        )

    def retry_all_missing_3mf(self) -> dict:
        return self.manager.retry_all_missing_3mf()
