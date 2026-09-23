from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from schemas import InputModel

Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")]
Text = Annotated[str, Field(min_length=1, max_length=1600)]
ALLOWED_HOSTS = {"www.cdc.gov", "www.fda.gov", "www.gov.uk", "assets.publishing.service.gov.uk", "www.nationalarchives.gov.uk", "www.nia.nih.gov", "www.mayoclinic.org", "www.acefitness.org", "www.nasm.org", "acsm.org", "repfitness.com", "builtwithscience.com", "rippedbody.com"}
ALLOWED_HOSTS.update({"kb.cybexintl.com", "support.lifefitness.com", "pubmed.ncbi.nlm.nih.gov"})


class Section(InputModel):
    id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=160)]
    locator: Text
    summary: Text
    keywords: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(min_length=1, max_length=20)


class Source(InputModel):
    id: Identifier
    title: Text
    original_title: Text
    publisher: Text
    url: str
    origin: Literal["public_web", "curated_text"] = "public_web"
    topic: Literal["nutrition", "training"]
    source_version: Text
    reviewed_on: date
    review_due: date
    status: Literal["approved", "withdrawn"]
    scope: Text
    license: Text
    license_url: str
    notice: Text
    sections: list[Section] = Field(min_length=1, max_length=100)

    @staticmethod
    def approved_url(value):
        url = urlsplit(value)
        if url.scheme != "https" or url.hostname not in ALLOWED_HOSTS or url.username or url.password or url.port:
            raise ValueError("Source URL is not approved")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.origin == "public_web":
            self.approved_url(self.url)
            self.approved_url(self.license_url)
        elif self.topic != "training" or self.url or self.license_url:
            raise ValueError("Curated training notes cannot claim fabricated original or license URLs")
        if not 1 <= (self.review_due - self.reviewed_on).days <= 366:
            raise ValueError("Source needs a bounded review period")
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("Duplicate section identifier")
        return self


class Corpus(InputModel):
    version: Annotated[str, Field(min_length=1, max_length=80)]
    sources: list[Source] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_sources(self):
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source identifier")
        return self
