"""CRUD for the memory bank entries attached to one adventure.

`app/memorybank.py` owns embedding and retrieval. These endpoints only edit the
rows.
"""

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session, load_only

from ... import limits, memorybank, models, schemas, tree
from ...context import lineage
from ...database import get_db

from .deps import CurrentUser, get_adventure_or_404, router


# The columns `schemas.MemoryOut` renders. `embedded` is a real column and
# belongs here. The vector it describes does not.
MEMORY_LIST_COLUMNS = (
    models.Memory.adventure_id,
    models.Memory.text,
    models.Memory.pinned,
    models.Memory.forgotten,
    models.Memory.embedded,
    models.Memory.use_count,
    models.Memory.last_used_at,
    models.Memory.source_start,
    models.Memory.source_end,
    models.Memory.created_at,
)


@router.get("/{adventure_id}/memories", response_model=list[schemas.MemoryOut])
def list_memories(
    adventure_id: int, db: Session = Depends(get_db), user: models.User = CurrentUser
):
    adventure = get_adventure_or_404(adventure_id, db, user)
    # Name the columns in a query rather than walk `adventure.memories`.
    # Retrieval used to walk the relationship, which is why a turn cost
    # megabytes: a relationship load returns whole entities, so it reads
    # whatever the model carries. `embedding_blob` is deferred and would stay
    # out today, so this rule is about the next wide column rather than that
    # one.
    #
    # The drawer shows the same bank the model reads. The filter uses the same
    # clause retrieval uses, so the drawer answers one question rather than two.
    # An adventure-wide list would show memories from branches this story never
    # went down, which are never retrieved, and a reader cannot tell those apart
    # from the ones in play. No memory becomes unreachable, because a memory
    # belongs to a branch: switching to that branch shows it, and deleting the
    # branch deletes its memories.
    return (
        db.query(models.Memory)
        .options(load_only(*MEMORY_LIST_COLUMNS))
        .filter(
            models.Memory.adventure_id == adventure_id,
            lineage.path_of(db, adventure).clause(models.Memory),
        )
        .order_by(models.Memory.id)
        .all()
    )


@router.post("/{adventure_id}/memories", response_model=schemas.MemoryOut, status_code=201)
def create_memory(
    adventure_id: int,
    payload: schemas.MemoryCreate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    """Adds a memory manually. The next post-turn pass embeds it."""
    adventure = get_adventure_or_404(adventure_id, db, user)
    limits.check_row_cap("memories", db, user, adventure=adventure)
    if not payload.text.strip():
        raise HTTPException(400, "Memory text cannot be empty")
    memory = models.Memory(adventure_id=adventure.id, text=payload.text.strip())
    # No node produced this memory, so it gets a branch but no depth.
    tree.place_memory(db, adventure, memory)
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


@router.patch("/{adventure_id}/memories/{memory_id}", response_model=schemas.MemoryOut)
def update_memory(
    adventure_id: int,
    memory_id: int,
    payload: schemas.MemoryUpdate,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    memory = db.get(models.Memory, memory_id)
    if memory is None or memory.adventure_id != adventure_id:
        raise HTTPException(404, "Memory not found")
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if "text" in fields and fields["text"].strip() != memory.text:
        memorybank.set_vector(memory, None)  # Re-embed on the next post-turn pass.
    for field, value in fields.items():
        setattr(memory, field, value)
    db.commit()
    return memory


@router.delete("/{adventure_id}/memories/{memory_id}", status_code=204)
def delete_memory(
    adventure_id: int,
    memory_id: int,
    db: Session = Depends(get_db),
    user: models.User = CurrentUser,
):
    get_adventure_or_404(adventure_id, db, user)
    memory = db.get(models.Memory, memory_id)
    if memory is None or memory.adventure_id != adventure_id:
        raise HTTPException(404, "Memory not found")
    db.delete(memory)
    db.commit()
