from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

app = FastAPI()

# in-memory DB
users = {}
user_id_seq = 1


class UserCreate(BaseModel):
    name: str
    email: str


@app.get("/users")
def list_users(limit: int = 10):
    return list(users.values())[:limit]


from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

app = FastAPI()

# in-memory DB
users = {}
user_id_seq = 1


class UserCreate(BaseModel):
    name: str
    email: str


@app.get("/users")
def list_users(limit: int = 10):
    return list(users.values())[:limit]


@app.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(user: UserCreate):
    global user_id_seq

    new_user = {
        "id": user_id_seq,
        "name": user.name,
        "email": user.email
    }

    users[user_id_seq] = new_user
    user_id_seq += 1

    return new_user


@app.get("/users/{user_id}")
def get_user(user_id: int):
    if user_id not in users:
        raise HTTPException(status_code=404, detail="User not found")
    return users[user_id]