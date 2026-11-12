from alembic import context
from api.db import Base, make_engine

def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()

connection = context.config.attributes.get('connection')
if connection is not None:
    run(connection)
else:
    engine = make_engine()
    with engine.connect() as connection:
        run(connection)
