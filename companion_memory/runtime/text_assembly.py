"""The independent fixed text-learning combination, without resource startup.

All command and repository identities are constructed before SQLite opens.
Transport, credentials, native management authority and readiness are bound by
the owning host later; creating declarations grants none of those capabilities.
"""
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.media.service import MediaService
from companion_memory.provider import LedgerAssembly,ProviderService
from companion_memory.self_model.transactions import PersonaTransactions
from companion_memory.goals.repository import goals_catalog
from companion_memory.state.repository import state_catalog
from companion_memory.retrieval.repository import retrieval_catalog
from companion_memory.information.initialization import InformationInitialization
from companion_memory.information.management import ManagementAssembly
from companion_memory.persistence import PersistenceService
from .content_assembly import ContentAssembly
from .candidate_goals import add_goal_commands


class TextLearningAssembly:
    """One statically declared combination with old local services retained."""
    def __init__(self):
        self.configuration=TextConfigurationAssembly();self.media=MediaService()
        self.content=ContentAssembly(self.media,self.media.repositories,information_format=True,text_format=True)
        self.ledger=LedgerAssembly(text_generation=True);self.provider=ProviderService(self.ledger)
        self.information_catalogs=(retrieval_catalog(),state_catalog(),goals_catalog())
        add_goal_commands(self.content,self.information_catalogs[2].definition)
        self.persona=PersonaTransactions(self.content,self.provider)
        domains=tuple(c.definition for c in self.information_catalogs)
        # Existing information commands do not acquire a self-model participant.
        original_participants=domains+tuple(r for r in self.content.repositories if r.owner_module!='self_model')
        self.initializer=InformationInitialization(original_participants,self.configuration.repositories[0])
        self.management=ManagementAssembly(original_participants)
        self.repositories=self.configuration.repositories+self.content.repositories+self.ledger.repositories+domains
        self.commands=self.configuration.commands+self.content.commands+self.ledger.commands+self.media.commands+self.initializer.commands+self.management.commands
        self.storage=PersistenceService(self.repositories,self.commands,assembly_format='MODEL_TEXT_LEARNING_V1')

    @property
    def static_carrier(self) -> bytes:
        """Borrow the immutable registered encoding; retain no second full copy."""
        return self.storage._assembly
