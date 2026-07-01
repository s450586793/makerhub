from __future__ import annotations

from fastapi import APIRouter

from app.api import config as config_api


router = APIRouter(prefix="/api")

router.add_api_route("/models", config_api.get_models_data, methods=["GET"])
router.add_api_route("/models/light", config_api.get_models_light_data, methods=["GET"])
router.add_api_route("/models/{model_dir:path}/comments", config_api.get_model_detail_comments, methods=["GET"])
router.add_api_route("/models/{model_dir:path}/download-all", config_api.download_model_all_files, methods=["GET"])
router.add_api_route("/models/{model_dir:path}/bambu-studio-link", config_api.create_bambu_studio_download_link, methods=["POST"])
router.add_api_route(
    "/public/bambu-studio/models/{model_dir:path}/files/{file_name}",
    config_api.public_bambu_studio_download_file,
    methods=["GET"],
)
router.add_api_route("/models/{model_dir:path}/attachments", config_api.upload_model_attachment, methods=["POST"])
router.add_api_route(
    "/models/{model_dir:path}/attachments/{attachment_id}",
    config_api.remove_model_attachment,
    methods=["DELETE"],
)
router.add_api_route(
    "/models/{model_dir:path}/local/description",
    config_api.update_local_model_description_data,
    methods=["PATCH"],
)
router.add_api_route(
    "/models/{model_dir:path}/local/metadata",
    config_api.update_local_model_metadata_data,
    methods=["PATCH"],
)
router.add_api_route("/models/{model_dir:path}/local/files", config_api.upload_local_model_files, methods=["POST"])
router.add_api_route("/models/{model_dir:path}/local/files", config_api.remove_local_model_file, methods=["DELETE"])
router.add_api_route("/models/{model_dir:path}/local/images", config_api.upload_local_model_images, methods=["POST"])
router.add_api_route("/models/{model_dir:path}/local/images", config_api.remove_local_model_image, methods=["DELETE"])
router.add_api_route(
    "/models/{model_dir:path}/local/images/cover",
    config_api.update_local_model_cover_image,
    methods=["PATCH"],
)
router.add_api_route(
    "/models/{model_dir:path}/local/preview-image",
    config_api.save_local_model_preview_image,
    methods=["POST"],
)
router.add_api_route(
    "/models/{model_dir:path}/local/preview-image/failure",
    config_api.save_local_model_preview_image_failure,
    methods=["POST"],
)
router.add_api_route("/models/delete", config_api.delete_models, methods=["POST"])
router.add_api_route("/models/flags", config_api.get_model_flags, methods=["GET"])
router.add_api_route("/models/flags/favorite", config_api.update_model_favorite, methods=["POST"])
router.add_api_route("/models/flags/printed", config_api.update_model_printed, methods=["POST"])
router.add_api_route("/models/flags/deleted", config_api.update_model_deleted, methods=["POST"])
router.add_api_route("/models/{model_dir:path}", config_api.get_model_detail_data, methods=["GET"])
router.add_api_route("/local-library/merge", config_api.merge_local_library_models, methods=["POST"])
router.add_api_route("/local-library/import", config_api.import_local_library_files, methods=["POST"])
