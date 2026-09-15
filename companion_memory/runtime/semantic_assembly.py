"""Complete semantic static declarations before any database or network opens.

The original content and information owners retain their repositories and local
commands. The semantic format selects the fixed-source memory branch, native
embedding ledger and all work/index/cache declarations as one immutable unit.
"""
from companion_memory.configuration.semantic_persistence import SemanticConfigurationAssembly
from companion_memory.media.service import MediaService
from companion_memory.provider import LedgerAssembly,ProviderService
from companion_memory.persistence import PersistenceService,UnitOfWork
from companion_memory.persistence.semantic_commands import SemanticCommands
from companion_memory.persistence.semantic_records import Record
from companion_memory.persistence.text_records import extend_catalog
from companion_memory.memory.initial_self_commands import InitialSelfCommands
from companion_memory.memory.initial_subjects import definition as subjects_definition,register_subjects
from companion_memory.retrieval.repository import retrieval_catalog
from companion_memory.retrieval.semantic_repository import semantic_retrieval_catalog
from companion_memory.retrieval.semantic_work import SemanticWork
from companion_memory.retrieval.semantic_generation import SemanticGenerations
from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.provider.embedding_service import EmbeddingProvider
from companion_memory.cognition.fixed_memory import FixedMemorySets
from collections.abc import Callable
from companion_memory.goals.repository import goals_catalog
from companion_memory.state.repository import state_catalog
from companion_memory.information.initialization import InformationInitialization
from companion_memory.information.management import ManagementAssembly
from .content_assembly import ContentAssembly
from .candidate_goals import add_goal_commands


class SemanticAssembly:
    """One immutable owner graph; unbound declarations cannot execute work."""
    def __init__(self, *, usage_only:bool=False):
        self.media=MediaService()
        self.content=ContentAssembly(self.media,self.media.repositories,information_format=True,semantic_format=True)
        self.retrieval_catalog=extend_catalog(retrieval_catalog(semantic_format=True),semantic_retrieval_catalog(),4)
        self.information_catalogs=(self.retrieval_catalog,state_catalog(),goals_catalog())
        catalogs={c.definition.owner_module:c for c in self.content.catalogs}
        self.configuration=SemanticConfigurationAssembly(catalogs['memory'],self.retrieval_catalog)
        self.ledger=LedgerAssembly(embedding_format=True,embedding_usage_only=usage_only)
        # The old runtime keeps its closed, uninitialized generation facade.
        # No generation capability or learning entry is issued by this host.
        self.local_provider=ProviderService(self.ledger)
        add_goal_commands(self.content,self.information_catalogs[2].definition)
        domains=tuple(c.definition for c in self.information_catalogs)
        participants=self.content.repositories+domains
        self.initializer=InformationInitialization(participants,self.configuration.repository.definition)
        self.management=ManagementAssembly(participants)
        self.initial_self=InitialSelfCommands(catalogs['memory'])
        self.initial_subjects=subjects_definition((catalogs['memory'].definition,),self._subjects)
        self.repositories=(self.configuration.repository.definition,)+self.content.repositories+self.ledger.repositories+domains
        self.semantic=SemanticCommands(self.repositories,self._semantic,usage_only=usage_only)
        self.commands=self.configuration.commands+self.content.commands+self.ledger.commands+self.media.commands+self.initializer.commands+self.management.commands+self.initial_self.commands+(self.initial_subjects,)+self.semantic.commands
        self.storage=PersistenceService(self.repositories,self.commands,assembly_format='ASYNC_SEMANTIC_V1')
        self.work:SemanticWork|None=None;self.generations:SemanticGenerations|None=None;self.cache:SemanticQueryCache|None=None
        self.embedding:EmbeddingProvider|None=None;self.fixed:FixedMemorySets|None=None
        self.subject_origin: str|None=None
        self.admit_normal:Callable[[UnitOfWork],None]|None=None

    def _subjects(self,uow: UnitOfWork,values: Record) -> object:
        if self.subject_origin is None or self.work is None:raise ValueError('Initial subject authority is not bound.')
        if self.admit_normal is None:raise ValueError('Native mode admission is not bound.')
        self.admit_normal(uow)
        return register_subjects(self.content.memory,uow,values,self.subject_origin,self.content.utc_now_us())

    def _semantic(self,kind: str,uow: UnitOfWork,envelope: Record,payload: Record) -> object:
        if kind.startswith('fixed_') or kind in ('bind','resume','begin_generation','append_page','confirm_page','seal_generation','publish_generation') or kind=='prepare' and payload['kind']=='EMBED':
            if self.admit_normal is None:raise ValueError('Native host mode admission is not bound.')
            self.admit_normal(uow)
        if kind.startswith('fixed_'):owner=self.fixed
        elif kind in ('store_embedding_handoff','confirm_embedding_handoff','retire_embedding_handoff'):owner=self.embedding
        elif kind in ('begin_generation','append_page','confirm_page','seal_generation','publish_generation','retire_page','generation_fail'):owner=self.generations
        elif kind in ('cache_bind','cache_expire','gc_page'):owner=self.cache
        else:owner=self.work
        if owner is None:raise ValueError('Semantic owner is not bound.')
        return owner.handle(kind,uow,envelope,payload)
