import json
import re
from django.db import transaction
from chatbot.utils.S3.s3_service import upload_file_to_s3, list_files_in_s3, delete_files_from_s3


def normalize_label(label: str) -> str:
    label = label.strip().lower()
    label = re.sub(r'[^a-z0-9-]+', '_', label)
    label = re.sub(r'_+', '_', label)
    return label.strip('_')


@transaction.atomic
def handle_translation_s3(instance):
    """
    Handles S3 upload + versioning (scoped per namespace + language + label)
    Also handles namespace/label changes properly
    """

    from chatbot.models import TranslationFile

    existing = None

    if instance.id:
        try:
            existing = TranslationFile.objects.get(id=instance.id)
        except TranslationFile.DoesNotExist:
            pass

    new_namespace = instance.namespace
    new_label = instance.label

    old_namespace = existing.namespace if existing else None
    old_label = existing.label if existing else None

    namespace_changed = existing and (old_namespace != new_namespace)
    label_changed = existing and (old_label != new_label)
    path_changed = namespace_changed or label_changed

    data_changed = not (existing and existing.data == instance.data)

    # CASE 1: NOTHING CHANGED
    if existing and not data_changed and not path_changed:
        instance.s3_key = existing.s3_key
        return instance

    # CASE 2: SAME PATH, ONLY DATA CHANGED → version bump
    if existing and not path_changed:
        match = re.search(r"v(\d+)\.json$", existing.s3_key or "")
        current_version = int(match.group(1)) if match else 0
        new_version = current_version + 1
    else:
        # CASE 3: NEW PATH (namespace/label changed OR new object)
        new_version = 1

    file_name = f"v{new_version}.json"

    uploaded_key = upload_file_to_s3(
        file_name=file_name,
        file_content=json.dumps(instance.data).encode("utf-8"),
        content_type="application/json",
        project_id=None,
        folder_structure=f"translations/{new_namespace}/{new_label}/{instance.language}/"
    )

    if not uploaded_key:
        raise Exception("S3 upload failed")

    instance.s3_key = uploaded_key

    # Cleanup current folder
    cleanup_old_versions(instance, keep_last_n=2)

    # Cleanup old folder if path changed
    if existing and path_changed:
        old_prefix = f"translations/{old_namespace}/{old_label}/{instance.language}/"
        old_files = list_files_in_s3(prefix=old_prefix)

        if old_files:
            keys = [f["Key"] for f in old_files]
            delete_files_from_s3(keys=keys)

    return instance


def extract_version(key: str) -> int:
    match = re.search(r"v(\d+)\.json$", key or "")
    return int(match.group(1)) if match else 0


def cleanup_old_versions(instance, keep_last_n=2):
    prefix = f"translations/{instance.namespace}/{instance.label}/{instance.language}/"

    files = list_files_in_s3(prefix=prefix)
    print("Files fetched:", len(files))
    if not files:
        return

    # only versioned JSON files
    files = [
        f for f in files
        if f["Key"].endswith(".json") and re.search(r"v\d+\.json$", f["Key"])
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
