"""Fixed configuration-owned SQL for immutable domains, snapshots and active identity.

Statements are bound at trusted assembly. No caller can select arbitrary tables,
change a published version, or repair missing configuration records implicitly.
"""
from dataclasses import dataclass
from .definitions import RepositoryDefinition,StatementDefinition,TableDefinition
from .schema import BoundedTextSchema,Field,RecordSchema,ScalarSchema

ID=ScalarSchema('identifier')
INT=ScalarSchema('integer')
TEXT=BoundedTextSchema(8192)


@dataclass(frozen=True,slots=True)
class ConfigurationRepository:
    definition: RepositoryDefinition
    statements: tuple[tuple[str,StatementDefinition],...]


def create_configuration_repository() -> ConfigurationRepository:
    tables=(
        TableDefinition('configuration_snapshots','CREATE TABLE configuration_snapshots(scope_id TEXT NOT NULL, snapshot_seq INTEGER NOT NULL CHECK(snapshot_seq>0), snapshot_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL, PRIMARY KEY(scope_id,snapshot_seq))'),
        TableDefinition('configuration_domains','CREATE TABLE configuration_domains(scope_id TEXT NOT NULL, domain_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), item_count INTEGER NOT NULL CHECK(item_count>0), digest TEXT NOT NULL, PRIMARY KEY(scope_id,domain_id,revision))'),
        TableDefinition('configuration_entries','CREATE TABLE configuration_entries(scope_id TEXT NOT NULL, domain_id TEXT NOT NULL, revision INTEGER NOT NULL, parameter_key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(scope_id,domain_id,revision,parameter_key), FOREIGN KEY(scope_id,domain_id,revision) REFERENCES configuration_domains(scope_id,domain_id,revision))'),
        TableDefinition('active_configuration','CREATE TABLE active_configuration(scope_id TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL REFERENCES configuration_snapshots(snapshot_id))'),
    )
    def stmt(sql,params,row,writes):
        return StatementDefinition(sql,RecordSchema(tuple(Field(k,s) for k,s in params)),RecordSchema(tuple(Field(k,s) for k,s in row)),writes)
    statements=(
        ('active',stmt('SELECT snapshot_id FROM active_configuration WHERE scope_id=:scope_id',(),(('snapshot_id',ID),),False)),
        ('snapshot',stmt('SELECT snapshot_id,body FROM configuration_snapshots WHERE scope_id=:scope_id AND snapshot_id=:snapshot_id',(('snapshot_id',ID),),(('snapshot_id',ID),('body',TEXT)),False)),
        ('allocate',stmt("INSERT INTO configuration_snapshots SELECT :scope_id, COALESCE(MAX(snapshot_seq),0)+1, 'configuration:'||(COALESCE(MAX(snapshot_seq),0)+1), :body FROM configuration_snapshots WHERE scope_id=:scope_id RETURNING snapshot_id",(('body',TEXT),),(('snapshot_id',ID),),True)),
        ('domain_insert',stmt('INSERT INTO configuration_domains VALUES(:scope_id,:domain_id,1,:item_count,:digest) RETURNING revision',(('domain_id',ID),('item_count',INT),('digest',ID)),(('revision',INT),),True)),
        ('entry_insert',stmt('INSERT INTO configuration_entries VALUES(:scope_id,:domain_id,1,:parameter_key,:body) RETURNING parameter_key',(('domain_id',ID),('parameter_key',BoundedTextSchema(8192)),('body',TEXT)),(('parameter_key',BoundedTextSchema(8192)),),True)),
        ('activate',stmt('INSERT INTO active_configuration VALUES(:scope_id,:snapshot_id) RETURNING snapshot_id',(('snapshot_id',ID),),(('snapshot_id',ID),),True)),
        ('domain',stmt('SELECT revision,item_count,digest FROM configuration_domains WHERE scope_id=:scope_id AND domain_id=:domain_id AND revision=:revision',(('domain_id',ID),('revision',INT)),(('revision',INT),('item_count',INT),('digest',ID)),False)),
        ('entry',stmt('SELECT parameter_key,body FROM configuration_entries WHERE scope_id=:scope_id AND domain_id=:domain_id AND revision=:revision AND parameter_key=:parameter_key',(('domain_id',ID),('revision',INT),('parameter_key',BoundedTextSchema(8192))),(('parameter_key',BoundedTextSchema(8192)),('body',TEXT)),False)),
        ('entry_keys',stmt('SELECT parameter_key FROM configuration_entries WHERE scope_id=:scope_id AND domain_id=:domain_id AND revision=:revision ORDER BY parameter_key LIMIT 1025',(('domain_id',ID),('revision',INT)),(('parameter_key',BoundedTextSchema(8192)),),False)),
    )
    return ConfigurationRepository(RepositoryDefinition('configuration',1,tables,tuple(s for _,s in statements)),statements)
