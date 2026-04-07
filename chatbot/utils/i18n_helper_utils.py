import json
import re
from django.db import transaction
from chatbot.utils.S3.s3_service import upload_file_to_s3, list_files_in_s3, delete_files_from_s3


def normalize_label(label: str) -> str:
    label = label.strip().lower()
    label = re.sub(r'[^a-z0-9]+', '_', label)
    label = re.sub(r'_+', '_', label)
    return label.strip('_')


@transaction.atomic
def handle_translation_s3(instance):
    """
    Handles S3 upload + versioning (scoped per namespace + language + label)
    """

    from chatbot.models import TranslationFile
    existing = None

    if instance.id:
        try:
            existing = TranslationFile.objects.get(id=instance.id)
        except TranslationFile.DoesNotExist:
            pass

    if existing and existing.data == instance.data:
        instance.s3_key = existing.s3_key
        return instance

    if existing:
        match = re.search(r"_v(\d+)\.json$", existing.s3_key or "")
        current_version = int(match.group(1)) if match else 0
        new_version = current_version + 1
    else:
        new_version = 1

    file_name = f"{instance.language}_v{new_version}.json"

    uploaded_key = upload_file_to_s3(
        file_name=file_name,
        file_content=json.dumps(instance.data).encode("utf-8"),
        content_type="application/json",
        project_id=None,
        folder_structure=f"translations/{instance.namespace}/{instance.label}/",
    )

    if not uploaded_key:
        raise Exception("S3 upload failed")

    instance.s3_key = uploaded_key
    cleanup_old_versions(instance, keep_last_n=2)

    return instance


def extract_version(key: str) -> int:
    match = re.search(r"_v(\d+)\.json$", key)
    return int(match.group(1)) if match else 0


def cleanup_old_versions(instance, keep_last_n=2):
    prefix = f"translations/{instance.namespace}/{instance.label}/"

    files = list_files_in_s3(prefix=prefix)
    print("Files fetched:", len(files))
    if not files:
        return

    # only versioned JSON files
    files = [
        f for f in files
        if f["Key"].endswith(".json") and "_v" in f["Key"]
    ]

    # sort DESC
    files_sorted = sorted(
        files,
        key=lambda x: extract_version(x["Key"]),
        reverse=True
    )

    # keep latest N
    files_to_delete = files_sorted[keep_last_n:]

    print("Files deleted:", len(files_to_delete))
    if not files_to_delete:
        return

    keys_to_delete = [f["Key"] for f in files_to_delete]
    delete_files_from_s3(keys=keys_to_delete)
