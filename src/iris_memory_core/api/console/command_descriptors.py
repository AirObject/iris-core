"""Explicit, versioned form metadata for implemented management commands."""

from __future__ import annotations

import json
from typing import Any


def with_commands(collection: str, value: dict[str, Any], *, writable: bool) -> dict[str, Any]:
    if collection == "cognitive-events" and writable:
        value["actions"] = [
            {
                "id": "dismiss",
                "label": "取消投递",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [],
                "description": "取消待投递或已投递事件; 保留投递历史, 不产生 ACK 或完成关联任务。",
            }
        ]
    if collection in {"entities", "identities", "bindings"} and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        identity_kind = {"entities": "Entity", "identities": "Identity", "bindings": "Binding"}[
            collection
        ]
        request_name = "Console" + identity_kind + "CreateRequest"
        fields_name = "Console" + identity_kind + "CreateFields"
        schema = {**schemas[request_name], "$defs": {fields_name: schemas[fields_name]}}
        value["create_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/{request_name}.schema.json"
        )
        identity_fields = {
            "entities": [
                {
                    "key": "kind",
                    "label": "实体类型",
                    "type": "enum",
                    "required": True,
                    "options": list(
                        schemas["ConsoleEntityCreateFields"]["properties"]["kind"]["enum"]
                    ),
                },
                {"key": "display_name", "label": "实体名称", "type": "string", "required": True},
                {"key": "privacy_labels", "label": "隐私标签", "type": "json"},
            ],
            "identities": [
                {"key": "provider", "label": "身份提供方", "type": "string", "required": True},
                {"key": "realm", "label": "身份域", "type": "string", "required": True},
                {"key": "external_id", "label": "外部账号 ID", "type": "string", "required": True},
                {
                    "key": "entity_id",
                    "label": "初始关联实体",
                    "type": "lookup",
                    "lookup": "entities",
                },
            ],
            "bindings": [
                {
                    "key": "external_identity_id",
                    "label": "外部身份",
                    "type": "lookup",
                    "lookup": "identities",
                    "required": True,
                },
                {
                    "key": "entity_id",
                    "label": "目标实体",
                    "type": "lookup",
                    "lookup": "entities",
                    "required": True,
                },
            ],
        }
        value["create"] = {
            "id": "create",
            "label": {
                "entities": "新增实体",
                "identities": "注册外部身份",
                "bindings": "提出身份绑定",
            }[collection],
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "description": "身份数据按租户管理。绑定需显式确认。名称不用于自动合并。",
            "fields": identity_fields[collection],
        }
        if collection == "entities":
            value["actions"] = [
                {
                    "id": "redirect",
                    "label": "重定向实体",
                    "permission": "memory.write",
                    "method": "POST",
                    "description": (
                        "将当前身份解析指向明确选择的实体。历史身份保持原记录。此动作不能撤销。"
                    ),
                    "reason_codes": ["operator_request"],
                    "fields": [
                        {
                            "key": "target_id",
                            "label": "重定向目标实体",
                            "type": "lookup",
                            "lookup": "entities",
                            "required": True,
                        }
                    ],
                }
            ]
        if collection == "entities":
            value["actions"].insert(
                0,
                {
                    "id": "attributes",
                    "label": "记录实体属性",
                    "permission": "memory.write",
                    "method": "POST",
                    "description": "确认不会覆盖更高权威值。同级不同值会保留为冲突。",
                    "reason_codes": ["operator_request"],
                    "fields": [
                        {"key": "field", "label": "属性名称", "type": "string", "required": True},
                        {"key": "value", "label": "属性值", "type": "text", "required": True},
                        {
                            "key": "mode",
                            "label": "确认或明确更正",
                            "type": "enum",
                            "required": True,
                            "options": ["confirmation", "correction"],
                            "default": "confirmation",
                        },
                    ],
                },
            )
        if collection == "bindings":
            value["actions"] = [
                {
                    "id": action,
                    "label": label,
                    "permission": "memory.write",
                    "method": "POST",
                    "reason_codes": ["operator_request"],
                    "fields": [],
                }
                for action, label in (("confirm", "确认身份绑定"), ("revoke", "撤销身份绑定"))
            ]
    if collection == "artifacts" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        artifact_dependencies = (
            "ConsoleCommandScope",
            "ConsoleCommandSourceRef",
            "ConsoleArtifactCreateFields",
        )
        schema = {
            **schemas["ConsoleArtifactCreateRequest"],
            "$defs": {name: schemas[name] for name in artifact_dependencies},
        }
        value["create_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/ConsoleArtifactCreateRequest.schema.json"
        )
        value["create"] = {
            "id": "create",
            "label": "新增原始文本",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + [
                {
                    "key": "content",
                    "label": "原始文本",
                    "type": "text",
                    "required": True,
                    "description": (
                        "最多 256 KiB UTF-8。保存后内容不可修改。替换请创建新附件并更正引用者。"
                    ),
                },
                {
                    "key": "media_type",
                    "label": "文本格式",
                    "type": "enum",
                    "options": ["text/plain", "text/markdown"],
                    "default": "text/plain",
                },
                {
                    "key": "source_refs",
                    "label": "来源引用",
                    "type": "json",
                    "description": "至多一个当前可见的来源。",
                },
            ],
        }
    if collection == "artifacts" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        upload_dependencies = (
            "ConsoleCommandScope",
            "ConsoleCommandSourceRef",
            "ConsoleArtifactUploadFields",
        )
        schema = {
            **schemas["ConsoleArtifactUploadMetadata"],
            "$defs": {name: schemas[name] for name in upload_dependencies},
        }
        value["upload_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["upload_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["upload_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/ConsoleArtifactUploadMetadata.schema.json"
        )
        value["upload"] = {
            "id": "upload",
            "label": "上传原始附件",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "description": (
                "上传 1 字节至 8 MiB 的原始文件。内容保存后不可编辑。不会自动解析或执行。"
            ),
            "fields": [
                {
                    **field,
                    **(
                        {
                            "label": "附件媒体类型",
                            "options": list(
                                schemas["ConsoleArtifactUploadFields"]["properties"]["media_type"][
                                    "enum"
                                ]
                            ),
                            "default": "application/octet-stream",
                            "required": True,
                        }
                        if field["key"] == "media_type"
                        else {}
                    ),
                }
                for field in value["create"]["fields"]
                if field["key"] != "content"
            ]
            + [{"key": "privacy_labels", "label": "隐私标签", "type": "json"}],
        }
    if collection == "relations" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        relation_dependencies = (
            "ConsoleCommandScope",
            "ConsoleClaimEvidence",
            "ConsoleRelationCreateFields",
        )
        schema = {
            **schemas["ConsoleRelationCreateRequest"],
            "$defs": {name: schemas[name] for name in relation_dependencies},
        }
        value["create_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/ConsoleRelationCreateRequest.schema.json"
        )
        relation_fields = [
            {
                "key": "source_entity_id",
                "label": "起点实体",
                "type": "lookup",
                "lookup": "entities",
                "required": True,
            },
            {"key": "relation_type", "label": "关系类型", "type": "string", "required": True},
            {
                "key": "target_entity_id",
                "label": "终点实体",
                "type": "lookup",
                "lookup": "entities",
                "required": True,
            },
            {"key": "confidence", "label": "置信度 (0-1)", "type": "number"},
            {"key": "importance", "label": "重要度 (0-1)", "type": "number"},
            {"key": "accessibility", "label": "可访问度 (0-1)", "type": "number"},
            {"key": "valid_from_at", "label": "有效起点 (UTC)", "type": "string"},
            {"key": "valid_until_at", "label": "有效终点 (UTC)", "type": "string"},
            {
                "key": "evidence",
                "label": "证据引用",
                "type": "json",
                "required": True,
                "description": "最多 32 条有效证据; 创建只接受 supports, 更正会追加纠正证据。",
            },
        ]
        value["create"] = {
            "id": "create",
            "label": "新增关系",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + relation_fields,
        }
        value["actions"] = [
            {
                "id": "correct",
                "label": "更正关系",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": relation_fields,
            },
            {
                "id": "transition",
                "label": "变更关系状态",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "target_status",
                        "label": "目标状态",
                        "type": "enum",
                        "required": True,
                        "options": ["active", "disputed", "archived", "superseded", "retracted"],
                    },
                ],
            },
        ]
    if collection == "episodes" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        dependencies = (
            "ConsoleCommandScope",
            "ConsoleCommandSourceRef",
            "ConsoleEpisodeObservationRef",
            "ConsoleEpisodeCreateFields",
            "ConsoleEpisodeUpdateFields",
        )
        for mode in ("create", "update"):
            name = "ConsoleEpisode" + mode.title() + "Request"
            schema = {**schemas[name], "$defs": {key: schemas[key] for key in dependencies}}
            value[mode + "_schema"] = json.loads(
                json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
            )
            value[mode + "_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            value[mode + "_schema"]["$id"] = (
                f"https://schemas.iris-memory-core.local/console/{contract_version()}/{name}.schema.json"
            )
        editable_fields = [
            {"key": "title", "label": "片段标题", "type": "string", "required": True},
            {"key": "summary", "label": "片段摘要", "type": "text"},
            {
                "key": "participant_entity_ids",
                "label": "参与实体 ID 列表",
                "type": "json",
                "description": "最多 64 个当前可见的实体。",
            },
            {
                "key": "observation_refs",
                "label": "观察引用",
                "type": "json",
                "description": "最多 64 条 Observation 引用, 不修改原观察。",
            },
            {
                "key": "source_refs",
                "label": "来源引用",
                "type": "json",
                "description": "所有实体与来源合计最多 100 个不同引用。",
            },
            {"key": "importance", "label": "重要度 (0-1)", "type": "number"},
            {"key": "valence", "label": "情绪效价 (-1 至 1)", "type": "number"},
            {"key": "arousal", "label": "唤醒度 (0-1)", "type": "number"},
            {"key": "started_at", "label": "开始时间 (UTC)", "type": "string"},
            {"key": "ended_at", "label": "结束时间 (UTC)", "type": "string"},
        ]
        value["create"] = {
            "id": "create",
            "label": "新增片段",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + editable_fields,
        }
        value["actions"] = [
            {
                "id": "update",
                "label": "编辑片段",
                "permission": "memory.write",
                "method": "PATCH",
                "reason_codes": ["operator_request"],
                "fields": editable_fields,
            },
            {
                "id": "transition",
                "label": "变更片段状态",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "target_status",
                        "label": "目标状态",
                        "type": "enum",
                        "required": True,
                        "options": ["open", "sealed", "archived", "superseded"],
                    },
                ],
            },
        ]
    if collection == "claims" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        claim_defs = ("ConsoleCommandScope", "ConsoleClaimCreateFields", "ConsoleClaimEvidence")
        claim_schema = {
            **schemas["ConsoleClaimCreateRequest"],
            "$defs": {name: schemas[name] for name in claim_defs},
        }
        value["create_schema"] = json.loads(
            json.dumps(claim_schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/ConsoleClaimCreateRequest.schema.json"
        )
        evidence_field = {
            "key": "evidence",
            "label": "证据引用",
            "type": "json",
            "description": "引用当前可见的 Observation/Artifact/Episode/Claim/Note, 最多 32 条。",
        }
        value["create"] = {
            "id": "create",
            "label": "新增主张",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + [
                {
                    "key": "subject_entity_id",
                    "label": "主张主体",
                    "type": "lookup",
                    "lookup": "entities",
                },
                {
                    "key": "subject_is_self",
                    "label": "关于该 Agent 自身",
                    "type": "boolean",
                    "default": False,
                    "description": "选择具体主体或勾选自身, 两者选一。",
                },
                {"key": "predicate", "label": "谓词", "type": "string", "required": True},
                {"key": "value", "label": "主张值", "type": "json", "required": True},
                {"key": "canonical_text", "label": "主张文本", "type": "text"},
                {
                    "key": "category",
                    "label": "主张类别",
                    "type": "enum",
                    "options": schemas["ConsoleClaimCreateFields"]["properties"]["category"][
                        "enum"
                    ],
                },
                {"key": "confidence", "label": "置信度 (0-1)", "type": "number"},
                {"key": "importance", "label": "重要度 (0-1)", "type": "number"},
                {"key": "accessibility", "label": "可访问度 (0-1)", "type": "number"},
                {"key": "valid_from_at", "label": "有效起点 (UTC)", "type": "string"},
                {"key": "valid_until_at", "label": "有效终点 (UTC)", "type": "string"},
                {**evidence_field, "required": True},
            ],
        }
        value["actions"] = [
            {
                "id": "correct",
                "label": "更正或撤回主张",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "mode",
                        "label": "处理方式",
                        "type": "enum",
                        "required": True,
                        "options": ["supersede", "dispute", "retract"],
                        "description": "supersede 更正内容, dispute 标记争议, retract 撤回。",
                    },
                    {"key": "value", "label": "更正后的值", "type": "json"},
                    {"key": "canonical_text", "label": "更正后的文本", "type": "text"},
                    {
                        **evidence_field,
                        "description": "更正或争议必须给出有效证据引用, 撤回可省略。",
                    },
                ],
            }
        ]
    if collection == "observations" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        observation_names = ("ConsoleCommandScope", "ConsoleObservationCreateFields")
        schema = {
            **schemas["ConsoleObservationCreateRequest"],
            "$defs": {name: schemas[name] for name in observation_names},
        }
        value["create_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/ConsoleObservationCreateRequest.schema.json"
        )
        value["create"] = {
            "id": "create",
            "label": "记录当前人工提交",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + [
                {
                    "key": "content",
                    "label": "当前提交内容",
                    "type": "text",
                    "required": True,
                    "description": (
                        "记录本次提交, 历史消息请通过审核导入。提交时间与操作者由服务端记录。"
                    ),
                }
            ],
        }
        value["actions"] = [
            {
                "id": "annotate",
                "label": "添加注释便签",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [
                    {"key": "title", "label": "注释标题", "type": "string", "required": True},
                    {
                        "key": "body",
                        "label": "注释正文",
                        "type": "text",
                        "required": True,
                        "allow_empty": True,
                        "description": "创建关联便签, 保留原始观察与发生时身份。",
                    },
                    {"key": "importance", "label": "重要度 (0-1)", "type": "number"},
                ],
            }
        ]
    if collection == "notes" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        names = ("ConsoleCommandScope", "ConsoleCommandSourceRef", "ConsoleNoteCreateFields")
        # Publish a bounded, self-contained schema, not the whole OpenAPI tree.
        schema = {
            **schemas["ConsoleNoteCreateRequest"],
            "$defs": {name: schemas[name] for name in names},
        }
        schema = json.loads(json.dumps(schema).replace("#/components/schemas/", "#/$defs/"))
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/"
            "ConsoleNoteCreateRequest.schema.json"
        )
        value["create_schema"] = schema
        value["create"] = {
            "id": "create",
            "label": "新增便签",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + [
                {
                    "key": "kind",
                    "label": "便签类型",
                    "type": "enum",
                    "required": True,
                    "options": schemas["ConsoleNoteCreateFields"]["properties"]["kind"]["enum"],
                },
                {"key": "title", "label": "标题", "type": "string", "required": True},
                {"key": "body", "label": "正文", "type": "text", "allow_empty": True},
                {"key": "importance", "label": "重要度 (0-1)", "type": "number"},
            ],
        }
        update_schema = {
            **schemas["ConsoleNoteUpdateRequest"],
            "$defs": {"ConsoleNoteUpdateFields": schemas["ConsoleNoteUpdateFields"]},
        }
        value["update_schema"] = json.loads(
            json.dumps(update_schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["update_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["update_schema"]["$id"] = (
            f"https://schemas.iris-memory-core.local/console/{contract_version()}/ConsoleNoteUpdateRequest.schema.json"
        )
        value["actions"] = [
            {
                "id": "update",
                "label": "编辑便签",
                "permission": "memory.write",
                "method": "PATCH",
                "reason_codes": ["operator_request"],
                "fields": value["create"]["fields"][-3:],
            },
            {
                "id": "transition",
                "label": "变更便签状态",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "target_status",
                        "label": "目标状态",
                        "type": "enum",
                        "required": True,
                        "options": ["inbox", "pinned", "snoozed", "archived", "promoted"],
                    },
                    {
                        "key": "snooze_until_at",
                        "label": "推迟至 (UTC)",
                        "type": "string",
                        "description": "仅 snoozed 必填, 格式 YYYY-MM-DDTHH:mm:ss.ffffffZ",
                    },
                    {
                        "key": "promotion_target_type",
                        "label": "提升为",
                        "type": "enum",
                        "options": ["task", "claim", "episode"],
                        "description": "仅 promoted 必填",
                    },
                ],
            },
        ]
    if collection == "tasks" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        for verb in ("Create", "Update"):
            task_defs = ["ConsoleTask" + verb + "Fields"]
            if verb == "Create":
                task_defs += ["ConsoleCommandScope", "ConsoleCommandSourceRef"]
            name = "ConsoleTask" + verb + "Request"
            schema = {**schemas[name], "$defs": {key: schemas[key] for key in task_defs}}
            schema = json.loads(json.dumps(schema).replace("#/components/schemas/", "#/$defs/"))
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            schema["$id"] = (
                f"https://schemas.iris-memory-core.local/console/{contract_version()}/{name}.schema.json"
            )
            value[verb.lower() + "_schema"] = schema
        fields = [
            {"key": "title", "label": "标题", "type": "string", "required": True},
            {"key": "goal", "label": "目标", "type": "text", "allow_empty": True},
            {"key": "priority", "label": "优先级 (0-9)", "type": "number"},
            {"key": "next_action", "label": "下一步", "type": "text", "allow_empty": True},
            {
                "key": "due_at",
                "label": "截止时间 (UTC)",
                "type": "string",
                "description": "格式 YYYY-MM-DDTHH:mm:ss.ffffffZ",
            },
        ]
        value["create"] = {
            "id": "create",
            "label": "新增任务",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + fields
            + [
                {
                    "key": "owner_kind",
                    "label": "负责人类型",
                    "type": "enum",
                    "options": ["agent", "joint", "entity", "space_group"],
                },
                {
                    "key": "owner_entity_id",
                    "label": "负责人实体",
                    "type": "lookup",
                    "lookup": "entities",
                    "description": "entity 必填; joint 可选",
                },
            ],
        }
        value["actions"] = [
            {
                "id": "update",
                "label": "编辑任务",
                "method": "PATCH",
                "permission": "memory.write",
                "reason_codes": ["operator_request"],
                "fields": [
                    *fields,
                    {
                        "key": "progress_note",
                        "label": "进展记录",
                        "type": "text",
                        "allow_empty": True,
                    },
                ],
            },
            {
                "id": "transition",
                "label": "变更任务状态",
                "method": "POST",
                "permission": "memory.write",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "target_status",
                        "label": "目标状态",
                        "type": "enum",
                        "required": True,
                        "options": [
                            "active",
                            "waiting",
                            "blocked",
                            "completed",
                            "cancelled",
                            "archived",
                        ],
                    },
                    {
                        "key": "completion_evidence_refs",
                        "label": "完成证据 (JSON)",
                        "type": "json",
                        "description": (
                            "仅 completed 必填: Observation 或 Artifact 引用数组, "
                            "含 resource_type、resource_id, 可选 revision"
                        ),
                    },
                ],
            },
        ]
    if collection == "focus-items" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        for verb in ("Create", "Update"):
            focus_defs = ["ConsoleFocus" + verb + "Fields"]
            if verb == "Create":
                focus_defs += ["ConsoleCommandScope", "ConsoleCommandSourceRef"]
            name = "ConsoleFocus" + verb + "Request"
            schema = {**schemas[name], "$defs": {key: schemas[key] for key in focus_defs}}
            schema = json.loads(json.dumps(schema).replace("#/components/schemas/", "#/$defs/"))
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            schema["$id"] = (
                f"https://schemas.iris-memory-core.local/console/{contract_version()}/{name}.schema.json"
            )
            value[verb.lower() + "_schema"] = schema
        fields = [
            {"key": "summary", "label": "摘要", "type": "text", "required": True},
            {"key": "salience", "label": "显著度 (0-1)", "type": "number"},
            {"key": "importance", "label": "重要度 (0-1)", "type": "number"},
            {"key": "structured_payload", "label": "结构化内容 (JSON)", "type": "json"},
        ]
        value["create"] = {
            "id": "create",
            "label": "新增关注",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + [
                {
                    "key": "kind",
                    "label": "关注类型",
                    "type": "enum",
                    "required": True,
                    "options": schemas["ConsoleFocusCreateFields"]["properties"]["kind"]["enum"],
                }
            ]
            + fields,
        }
        value["actions"] = [
            {
                "id": "update",
                "label": "编辑关注",
                "method": "PATCH",
                "permission": "memory.write",
                "reason_codes": ["operator_request"],
                "fields": fields,
            },
            {
                "id": "activate",
                "label": "激活关注",
                "method": "POST",
                "permission": "memory.write",
                "reason_codes": ["operator_request"],
                "fields": [],
            },
            {
                "id": "transition",
                "label": "变更关注状态",
                "method": "POST",
                "permission": "memory.write",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "target_status",
                        "label": "目标状态",
                        "type": "enum",
                        "required": True,
                        "options": ["dormant", "dismissed", "expired", "promoted"],
                    },
                    {
                        "key": "promotion_target_type",
                        "label": "提升目标",
                        "type": "enum",
                        "options": ["note", "task", "episode", "claim"],
                        "description": (
                            "仅提升时填写。Claim/Episode 需要有效原始证据。Task 创建为待确认计划。"
                        ),
                    },
                ],
            },
        ]
    if collection == "states" and writable:
        from iris_memory_core.api.console.contracts import contract_version, load_contract

        schemas = load_contract()["components"]["schemas"]
        for verb in ("Create", "Update"):
            state_defs = ["ConsoleState" + verb + "Fields"]
            if verb == "Create":
                state_defs.append("ConsoleCommandScope")
            schema_name = "ConsoleState" + verb + "Request"
            schema = {**schemas[schema_name], "$defs": {key: schemas[key] for key in state_defs}}
            schema = json.loads(json.dumps(schema).replace("#/components/schemas/", "#/$defs/"))
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            schema["$id"] = (
                f"https://schemas.iris-memory-core.local/console/{contract_version()}/{schema_name}.schema.json"
            )
            value[verb.lower() + "_schema"] = schema
        state_fields = [
            {"key": "value", "label": "状态值 (JSON)", "type": "json", "required": True},
            {
                "key": "ttl_us",
                "label": "有效时长 (微秒)",
                "type": "duration_us",
                "description": "省略使用默认时长; 0 表示不过期; 服务端执行时长上限",
            },
        ]
        value["create"] = {
            "id": "create",
            "label": "新增状态",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "initial_revision": 0,
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": lookup,
                    "required": key == "agent_id",
                }
                for key, label, lookup in (
                    ("agent_id", "所属 Agent", "agents"),
                    ("space_group_id", "空间组", "space-groups"),
                    ("space_id", "空间", "spaces"),
                    ("session_id", "会话", "sessions"),
                )
            ]
            + [
                {
                    "key": "namespace",
                    "label": "命名空间",
                    "type": "string",
                    "required": True,
                    "description": "只允许 user 来源的命名空间; runtime/environment 不接受人工写入",
                },
                {"key": "key", "label": "状态键", "type": "string", "required": True},
            ]
            + state_fields,
        }
        value["actions"] = [
            {
                "id": "update",
                "label": "更正状态",
                "permission": "memory.write",
                "method": "PATCH",
                "reason_codes": ["operator_request"],
                "fields": state_fields,
            },
            {
                "id": "expire",
                "label": "使状态过期",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [],
            },
        ]
    return value


def step_descriptor(*, writable: bool) -> dict[str, Any]:
    """Task child forms are published by the authorized parent collection."""
    from iris_memory_core.api.console.contracts import load_contract

    schemas = load_contract()["components"]["schemas"]
    fields = [
        {"key": "stable_key", "label": "步骤键", "type": "string", "required": True},
        {"key": "title", "label": "步骤标题", "type": "string", "required": True},
        {"key": "description", "label": "步骤说明", "type": "text", "allow_empty": True},
        {"key": "ordinal", "label": "顺序", "type": "number"},
        {"key": "expected_effect", "label": "预期外部效果", "type": "text"},
        {"key": "privacy_labels", "label": "步骤隐私标签 (JSON)", "type": "json"},
    ]
    value: dict[str, Any] = {
        "collection": "steps",
        "resource_type": "task_step",
        "label": "步骤",
        "list_columns": [],
        "filters": [],
        "sorts": [],
        "create_schema": None,
        "update_schema": None,
        "actions": [],
        "supports": {"history": False, "references": False, "forget": False},
    }
    if writable:
        schema = {
            **schemas["ConsoleTaskStepCreateRequest"],
            "$defs": {"ConsoleTaskStepCreateFields": schemas["ConsoleTaskStepCreateFields"]},
        }
        value["create_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create"] = {
            "id": "create",
            "label": "新增步骤",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": fields,
        }
        value["actions"] = [
            {
                "id": "transition",
                "label": "变更步骤状态",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [
                    {
                        "key": "target_status",
                        "label": "目标状态",
                        "type": "enum",
                        "required": True,
                        "options": schemas["ConsoleTaskStepTransitionRequest"]["properties"][
                            "target_status"
                        ]["enum"],
                    },
                    {
                        "key": "completion_evidence_refs",
                        "label": "完成证据 (JSON)",
                        "type": "json",
                        "description": "完成外部效果需 Observation/Artifact 引用数组",
                    },
                ],
            }
        ]
    return value


def dependency_descriptor(task_id: str, *, writable: bool) -> dict[str, Any]:
    from iris_memory_core.api.console.contracts import load_contract

    schemas = load_contract()["components"]["schemas"]
    value = step_descriptor(writable=False)
    value.update(collection="dependencies", resource_type="task_dependency", label="依赖")
    if writable:
        schema = {
            **schemas["ConsoleTaskDependencyCreateRequest"],
            "$defs": {
                "ConsoleTaskDependencyCreateFields": schemas["ConsoleTaskDependencyCreateFields"]
            },
        }
        value["create_schema"] = json.loads(
            json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
        )
        value["create_schema"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        value["create"] = {
            "id": "create",
            "label": "新增依赖",
            "permission": "memory.write",
            "method": "POST",
            "reason_codes": ["operator_request"],
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "type": "lookup",
                    "lookup": "task-steps",
                    "lookup_parent_id": task_id,
                    "required": True,
                }
                for key, label in (
                    ("predecessor_step_id", "前置步骤"),
                    ("successor_step_id", "后继步骤"),
                )
            ]
            + [
                {
                    "key": "condition",
                    "label": "满足条件",
                    "type": "enum",
                    "options": ["completed", "completed_or_skipped"],
                }
            ],
        }
        value["actions"] = [
            {
                "id": "remove",
                "label": "解除依赖",
                "permission": "memory.write",
                "method": "POST",
                "reason_codes": ["operator_request"],
                "fields": [],
            }
        ]
    return value


def trigger_descriptor(task_id: str, *, writable: bool) -> dict[str, Any]:
    from iris_memory_core.api.console.contracts import load_contract

    schemas = load_contract()["components"]["schemas"]
    value = step_descriptor(writable=False)
    value.update(collection="triggers", resource_type="task_trigger", label="触发器")
    if not writable:
        return value
    fields: list[dict[str, Any]] = [
        {
            "key": "kind",
            "label": "触发类型",
            "type": "enum",
            "required": True,
            "options": [
                "at_time",
                "recurrence",
                "observation_kind",
                "state_condition",
                "task_transition",
            ],
        },
        {
            "key": "task_step_id",
            "label": "关联步骤",
            "type": "lookup",
            "lookup": "task-steps",
            "lookup_parent_id": task_id,
            "description": "可留空。编辑时清空即可解除步骤关联",
        },
        {
            "key": "schedule_spec",
            "label": "时间计划 (JSON)",
            "type": "json",
            "description": (
                "at_time 使用 at_us。recurrence 使用 every_seconds 或 daily/at。条件触发留空或 null"
            ),
        },
        {
            "key": "condition_spec",
            "label": "触发条件 (JSON)",
            "type": "json",
            "description": "仅支持观测类型、State 比较和 Task/Step 状态条件。时间触发留空或 null",
        },
        {"key": "timezone", "label": "时区", "type": "string"},
        {
            "key": "catch_up_policy",
            "label": "错过触发后的处理",
            "type": "enum",
            "options": ["all", "latest", "coalesce", "skip"],
        },
        {"key": "misfire_grace_us", "label": "允许延迟(微秒)", "type": "number"},
        {"key": "max_occurrences_per_run", "label": "每轮触发上限", "type": "number"},
        {"key": "enabled", "label": "启用", "type": "boolean"},
    ]
    defaults = {
        "timezone": "UTC",
        "catch_up_policy": "all",
        "misfire_grace_us": 86400000000,
        "max_occurrences_per_run": 100,
        "enabled": True,
    }
    for field in fields:
        if field["key"] in defaults:
            field["default"] = defaults[field["key"]]
    for operation, slot in (("Create", "create_schema"), ("Update", "update_schema")):
        schema = {
            **schemas["ConsoleTaskTrigger" + operation + "Request"],
            "$defs": {"ConsoleTaskTriggerFields": schemas["ConsoleTaskTriggerFields"]},
        }
        value[slot] = json.loads(json.dumps(schema).replace("#/components/schemas/", "#/$defs/"))
        value[slot]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    common = {"permission": "memory.write", "reason_codes": ["operator_request"]}
    value["create"] = {
        **common,
        "id": "create",
        "label": "新增触发器",
        "method": "POST",
        "fields": fields,
    }
    value["actions"] = [
        {
            **common,
            "id": "update",
            "label": "编辑触发器",
            "method": "PATCH",
            "fields": fields,
            "description": "保存后按新配置重新安排触发。条件触发器可再次匹配当前条件。",
        },
        {
            **common,
            "id": "enabled",
            "label": "设置启用状态",
            "method": "POST",
            "fields": [{"key": "enabled", "label": "启用", "type": "boolean", "required": True}],
        },
    ]
    return value
