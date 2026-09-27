"""Production setup values and a compact projection of genuine operator input.

Resource identities are derived from shipped instructions and schemas. Local
account aliases, limits and role links do not attest to supplier eligibility,
credential availability, prices or permission to send. This builder is used only
for new drafts; saved and imported value documents remain explicit inputs.
"""
from __future__ import annotations

from hashlib import sha256
from typing import cast

from companion_memory.cognition import daily_resources, dream_resources
from companion_memory.retrieval.semantic_material import RENDER_DIGEST, space_identity
from companion_memory.self_model.request_material import PERSONA_TRANSFORM_RESOURCE
from .dream_schema import DREAM_ROLES, GENERATION_ROLES, ROLES
from .managed_validation import TOKEN_RATE_FIELDS

DEFAULTS_REVISION = 'managed_setup_v1'
GENERATION_ACCOUNT = 'generation_account'
EMBEDDING_ACCOUNT = 'embedding_account'
DEPLOYMENT_EPOCH = 'initial_embedding_deployment'
# Exact raw resources/embeddings.py bytes from the pinned official SDK source.
# This is client shape evidence, not a supplier capability or billing attestation.
SDK_EVIDENCE_SOURCE = 'https://raw.githubusercontent.com/volcengine/volcengine-python-sdk/3b116781c5b8bb648f215b29587f2ec74d9e5d02/volcenginesdkarkruntime/resources/embeddings.py'
SDK_EVIDENCE_DIGEST = 'e329090aab2037acadde43fe9fb40ab1bcbe4a7d13e90e2f396e19948f88c31b'
BASIC_DEFAULTS = {
    'role_name': 'Iris',
    'initial_material': '我的初始自我由外部设定；目前没有预设的个人经历、关系或特殊能力。'
        '我会区分外部设定、实际经历、他人陈述和推测，在后续经历中逐步形成自我认识。',
}
SUPERVISION_PROMPT = (
    '仅依据可核对的当前材料提炼稳定自我，不虚构经历、关系或能力。'
    '保留来源身份、世界边界和不确定性；外部设定不改写为亲历事实。'
    '当前情绪、活动和目标不直接作为稳定身份。重大变化须有明确依据，'
    '既有 persona 与同源重复不作为独立证据。不得自行更改生成目标或监管要求。'
)



def _record(values: dict[str, object], key: str) -> dict[str, object]:
    value = values[key]
    if type(value) is not dict:
        raise ValueError('Native setup record is required.')
    return cast(dict[str, object], value)


def _profile_id(role: str) -> str:
    return role.lower() + '_profile'


def _resource(role: str) -> dict[str, str]:
    owner = dream_resources if role in DREAM_ROLES else daily_resources
    return owner.resource_evidence(role, role.lower() + '_prompt', role.lower() + '_schema')


def populate_initial_values(values: dict[str, dict[str, object]], *, product: bool = False) -> None:
    """Complete a newly allocated native scaffold; leave real external facts empty.

    All aliases are stable across form rendering and idempotent draft creation.
    The deployment epoch is an explicit initial local binding, not a discovered
    supplier version. An operator changing the remote deployment must choose a
    new epoch and matching space through complete configuration validation.
    """
    foundation, text = values['foundation'], values['text']
    price: dict[str, object] = {
        'revision_ref': 'generation_price', 'source_url': None, 'checked_date': None,
        **{field: None for field in TOKEN_RATE_FIELDS}, 'per_attempt_money_bound': None,
    }
    foundation['provider.accounts'] = [
        {'account_id': GENERATION_ACCOUNT, 'window_id': 'generation_window',
         'currency': 'CNY', 'max_in_flight': 1, 'attempt_limit': 16,
         'cost_limit_atoms': 5000000, 'atom_scale': 1000000,
         'billing_mode': 'TOKEN_METERED', 'price': price, 'quota': None, 'evidence_ref': None},
        {'account_id': EMBEDDING_ACCOUNT, 'window_id': 'embedding_window',
         'currency': 'CNY', 'max_in_flight': 1, 'attempt_limit': 16,
         'cost_limit_atoms': None, 'atom_scale': 1000000,
         'billing_mode': 'USAGE_ONLY_TRIAL', 'price': None, 'quota': None, 'evidence_ref': None},
    ]
    if product:
        # Ordinary use records measured usage without inventing supplier prices
        # or imposing a lifetime trial ceiling. Per-request limits remain active.
        for account in cast(list[dict[str, object]], foundation['provider.accounts']):
            account.update(billing_mode='USAGE_ONLY', price=None, cost_limit_atoms=None,
                attempt_limit=None, evidence_ref='local_user_configuration')
    space = space_identity('ARK_CODING_DENSE_TEXT_V1',
        'https://ark.cn-beijing.volces.com/api/coding/v3/embeddings',
        'doubao-embedding-vision', DEPLOYMENT_EPOCH)
    profiles: list[dict[str, object]] = []
    for role in ROLES:
        embedding = role.startswith('EMBEDDING')
        profile: dict[str, object] = {
            'profile_id': _profile_id(role), 'account_id': EMBEDDING_ACCOUNT if embedding else GENERATION_ACCOUNT,
            'max_attempts': 1, 'attempt_timeout_ms': 30000,
            'model_id': 'doubao-embedding-vision' if embedding else 'deepseek-flash',
            'wire_protocol': 'ARK_CODING_DENSE_TEXT_V1' if embedding else
                'DEEPSEEK_IMAGE_JSON_V1' if role == 'MEDIA' else 'DEEPSEEK_CHAT_JSON_V1',
            'capability': 'EMBEDDING' if embedding else 'MEDIA_UNDERSTANDING' if role == 'MEDIA' else 'GENERATION',
            'max_input_units': None if embedding else 1048576,
            'max_output_units': 0 if embedding else {'MEDIA': 512, 'GOAL_DEDUP': 512, 'PERSONA': 2048}.get(role, 4096),
            'max_items': 1 if embedding or role == 'MEDIA' else 8,
            'dimensions': 1024 if embedding else None, 'space_id': space if embedding else None,
            'media_tasks': ['DESCRIBE'] if role == 'MEDIA' else [],
            'billing_mode': 'USAGE_ONLY' if product else 'USAGE_ONLY_TRIAL' if embedding else 'TOKEN_METERED', 'material_role': role,
        }
        profile['embedding_ref' if embedding else 'image_ref' if role == 'MEDIA' else 'generation_ref'] = (
            'provider.embedding_transport' if embedding else 'media.image_understanding' if role == 'MEDIA' else 'provider.generation')
        profiles.append(profile)
    foundation['provider.profiles'] = profiles
    foundation['provider.role_profiles'] = {role: [_profile_id(role)] for role in ROLES}
    resources: list[dict[str, object]] = [
        {'role': role, 'profile_id': _profile_id(role),
         'protocol': 'DEEPSEEK_IMAGE_JSON_V1' if role == 'MEDIA' else 'DEEPSEEK_CHAT_JSON_V1',
         **_resource(role)} for role in GENERATION_ROLES]
    _record(text, 'provider.generation')['roles'] = resources
    _record(text, 'provider.transport')['roles'] = [
        {**resource, 'origin': 'https://api.deepseek.com', 'base_path': '',
         'endpoint_path': '/chat/completions', 'secret_ref': None, 'secret_revision': 'v1',
         'account_ref': GENERATION_ACCOUNT, 'connect_timeout_ms': 10000, 'read_timeout_ms': 30000,
         'response_max_bytes': 262144, 'headers_max_bytes': 16384, 'header_count': 100,
         'chunk_bytes': 8192, 'network_slots': 1, 'queue_slots': 0} for resource in resources]
    for role, key in (('MEDIA', 'media.image_understanding'), ('GOAL_DEDUP', 'goals.semantic_deduplication'),
                      ('PERSONA', 'self_model.initial_persona')):
        selected = _record(text, key)
        selected.update(_resource(role))
        if role != 'PERSONA':
            selected['profile_id'] = _profile_id(role)
    _record(text, 'media.image_understanding').update(protocol='DEEPSEEK_IMAGE_JSON_V1', image_formats=['PNG', 'JPEG'])
    _record(text, 'self_model.initial_persona').update(
        transform_ref='initial_persona_output', transform_digest=sha256(PERSONA_TRANSFORM_RESOURCE).hexdigest(),
        supervision_prompt=SUPERVISION_PROMPT)
    refs = [_profile_id('EMBEDDING_DOCUMENT'), _profile_id('EMBEDDING_QUERY')]
    _record(text, 'retrieval.embedding').update(document_profile=refs[0], query_profile=refs[1], render_digest=RENDER_DIGEST)
    _record(text, 'retrieval.semantic').update(space_id=space, deployment_epoch=DEPLOYMENT_EPOCH)
    _record(text, 'provider.embedding_transport').update(
        profile_refs=refs, expected_reported_models=['doubao-embedding-vision', 'doubao-embedding-vision-251215'],
        sdk_evidence_digest=SDK_EVIDENCE_DIGEST, secret_revision='v1')
    if product:
        _record(text, 'provider.embedding_transport')['server_evidence_ref'] = 'configured_endpoint'
    links = _record(text, 'provider.dream_profiles')
    for role in DREAM_ROLES:
        _record(links, role)['profile_id'] = _profile_id(role)


def _address(domain: str, key: str, path: list[str | int]) -> dict[str, object]:
    return {'domain': domain, 'key': key, 'path': path}


def setup_presentation() -> dict[str, object]:
    """Describe editable genuine inputs without copying schemas or reading secrets.

    Paths refer to the existing native value document. Mirrors are visible shared
    bindings for the new default preset, not permission to rewrite arbitrary
    imported configurations. The browser must use the full form for other shapes.
    """
    fields: list[dict[str, object]] = []

    def field(domain: str, key: str, path: list[str | int], label: str, group: str, help_text: str,
              mirrors: list[dict[str, object]] | None = None, scale: int | None = None) -> None:
        item = {**_address(domain, key, path), 'label': label, 'group': group,
                'help': help_text, 'mirrors': mirrors or []}
        if scale is not None:
            item['scale'] = scale
        fields.append(item)

    for name, label in (('secret_ref', '凭据引用'), ('secret_revision', '凭据版本')):
        field('text', 'provider.transport', ['roles', 0, name], 'DeepSeek ' + label, '生成与图像',
            '填写受保护凭据文件的引用或版本；文件名为 引用__版本。这里只保存名称，不要输入 API key。'
            '七个生成与图像角色共用同一凭据；名称存在不代表文件已挂载。',
            [_address('text', 'provider.transport', ['roles', index, name]) for index in range(1, len(GENERATION_ROLES))])
    field('foundation', 'provider.accounts', [0, 'evidence_ref'], '生成账户依据标识', '生成与图像',
        '填写可追溯的本地账户使用声明标识；它不表示供应商资格或账单已经核实，也不授予发送许可。')
    for name, label in zip(TOKEN_RATE_FIELDS, ('未缓存输入单价', '缓存输入单价', '输出单价'), strict=True):
        field('foundation', 'provider.accounts', [0, 'price', name], label, '生成计费',
            '人民币元／百万 token；填写当前账户适用的真实费率，不用零表示未知。', scale=1000000)
    field('foundation', 'provider.accounts', [0, 'price', 'source_url'], '费率来源网址', '生成计费',
        '提供适用于当前账户的费率依据网址；配置记录依据，不自动联网核验。')
    field('foundation', 'provider.accounts', [0, 'price', 'checked_date'], '费率核实日期', '生成计费',
        '填写实际核实日期，格式 YYYY-MM-DD。')
    field('foundation', 'provider.accounts', [0, 'cost_limit_atoms'], '生成累计预算（元）', '生成计费',
        '默认 5 元是本地累计上限，不代表账单或发送许可。账户参数在业务初始化后固定；应在确认前检查。',
        scale=1000000)
    field('foundation', 'provider.accounts', [0, 'attempt_limit'], '生成尝试上限', '生成计费',
        '本地累计窗口最多 16 次作为默认；与 Embedding 上限之和不得超过 32，不代表供应商额度。')
    for name, label in (('secret_ref', '凭据引用'), ('secret_revision', '凭据版本')):
        field('text', 'provider.embedding_transport', [name], 'Embedding ' + label, 'Embedding',
            '填写受保护文件 引用__版本 的实际引用或版本；不输入 API key，不因配置名称存在就认为凭据可用。')
    field('foundation', 'provider.accounts', [1, 'evidence_ref'], '已分配用量依据标识', 'Embedding',
        '当前协议仅记录用量，不核算货币，不表示免费。填写本次账户已分配用量声明的可追溯标识；历史调用授权不延续。')
    field('text', 'provider.embedding_transport', ['server_evidence_ref'], '协议兼容依据标识', 'Embedding',
        '指向该 Coding 端点与模型的实际兼容依据及必要的用户决定，不表示已完成真实调用验证。')
    field('foundation', 'provider.accounts', [1, 'attempt_limit'], 'Embedding 尝试上限', 'Embedding',
        '默认本地累计上限 16 次；两个账户的上限之和不得超过 32。业务初始化后固定，发送仍需单独许可。')
    return {'defaults_revision': DEFAULTS_REVISION, 'basic_defaults': dict(BASIC_DEFAULTS), 'fields': fields}
