"""Bind paid query encoding to the authenticated request's quota."""
from services.usage import reserve_call


def request_retriever(request, user):
    retriever = request.app.state.knowledge
    if hasattr(retriever, "for_request"):
        return retriever.for_request(lambda: reserve_call(
            request.app.state.database, request.app.state.settings, user["id"]))
    return retriever
