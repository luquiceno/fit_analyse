"""API methods."""

import os
import json
from datetime import timedelta, datetime
from typing import Annotated, Optional

import msgpack

from fastapi import Body, Depends, FastAPI, HTTPException, File
from fastapi.responses import StreamingResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select

from app.auth import auth_handler
from app.auth import crypto
from app import model, model_helpers, fit_parsing

app_obj = FastAPI()
app_obj.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,  # Set to True if cookies are needed
    allow_methods=["*"],  # Allow all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allow all headers
)


# route handlers


@app_obj.post("/token")
async def login(
        form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
        session: Session = Depends(model_helpers.get_db_session)):
    """
    Login API to authenticate a user and generate an access token.

    This function takes user credentials from the request body
    and validates them against the database.
    If the credentials are valid, it generates an access token
    with a specific expiration time 
    and returns it along with the token type.

    Args:
        form_data: An instance of `OAuth2PasswordRequestForm` containing
            user credentials.
            Retrieved from the request body using Depends.
        session: A SQLAlchemy database session object. Obtained using
            Depends from `get_db_session`.

    Raises:
        HTTPException: If the username or password is incorrect (400 Bad Request).

    Returns:
        A `model.Token` object containing the access token and token type.
    """
    user = model.UserLogin(email=form_data.username,
                        password=form_data.password)
    db_user = auth_handler.check_and_get_user(user, session)
    if not db_user:
        raise HTTPException(
            status_code=400, detail="Incorrect username or password")
    time_out = int(os.getenv("TOKEN_TIMEOUT")) or 30
    token = auth_handler.create_access_token(db_user, timedelta(minutes=time_out))
    return model.Token(access_token=token, token_type="bearer")


@app_obj.post("/upload_activity", response_model=model.ActivityBase)
async def upload_activity(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    current_user_id: model.UserId = Depends(auth_handler.get_current_user_id),
    file: Annotated[bytes, File()]):
    ride_df = fit_parsing.extract_data_to_dataframe(file)
    summary = model_helpers.compute_activity_summary(ride_df=ride_df)
    activity_db = model.ActivityTable(
        activity_id=crypto.generate_random_base64_string(16),
        name="Ride",
        owner_id=current_user_id.id,
        distance=summary.distance,
        active_time=summary.active_time,
        elevation_gain=summary.elevation_gain,
        date=ride_df.timestamp.iloc[0],
        last_modified=datetime.now(),
        data=model_helpers.serialize_dataframe(ride_df)
    )
    session.add(activity_db)
    session.commit()
    session.refresh(activity_db)
    return activity_db

@app_obj.get("/activity/{activity_id}", response_model=model.ActivityResponse)
async def get_activity(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    activity_id: str):
    q = select(model.ActivityTable).where(
        model.ActivityTable.activity_id == activity_id)
    activity = session.exec(q).first()
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    activity_response = model_helpers.get_activity_response(activity, include_raw_data=False)
    return activity_response

@app_obj.get("/activity_map/{activity_id}")
async def get_activity_map(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    activity_id: str):
    activity = model_helpers.fetch_activity(activity_id, session)
    if not activity.static_map:
        activity_df = model_helpers.get_activity_df(activity)
        activity.static_map = model_helpers.get_activity_map(ride_df=activity_df, num_samples=200)
        if not activity.static_map:
            raise HTTPException(status_code=404, detail="GPS data not available")
        # Save the map for a future call
        session.add(activity)
        session.commit()
    return Response(activity.static_map, media_type="image/png")

@app_obj.get("/activity/{activity_id}/gpx")
async def get_activity_gpx_route(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    activity_id: str):
    activity = model_helpers.fetch_activity(activity_id, session)
    activity_df = model_helpers.get_activity_df(activity)
    gpx_content = model_helpers.get_activity_gpx(activity_df)

    def iterfile():
        yield gpx_content

    return StreamingResponse(
        iterfile(),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f"attachment; filename={activity_id}.gpx"}
    )

@app_obj.get("/activity/{activity_id}/raw")
async def get_activity_raw_columns(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    activity_id: str,
    columns: str = None):
    activity_df = model_helpers.fetch_activity_df(activity_id, session)
    activity_dict = activity_df.to_dict(orient="list")
    if columns:
        column_list = columns.split(",")
    else:
        column_list = [
            "timestamp", "power", "distance", "speed", "altitude",
            "position_lat", "position_long"]
    available_cols = set(activity_df.columns)
    activity_dict = {col: activity_dict[col] for col in column_list if col in available_cols}
    serialized_data = msgpack.packb(activity_dict)
    
    # Create a streaming response
    def generate_data():
        yield serialized_data

    return StreamingResponse(generate_data(), media_type="application/x-msgpack")

@app_obj.get("/activities", response_model=list[model.ActivityBase])
async def get_activities(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    current_user_id: model.UserId = Depends(auth_handler.get_current_user_id),
    limit: int = 10, # Default limit
    cursor_date: Optional[datetime] = None, # The 'date' of the last item seen
    cursor_id: Optional[str] = None # The 'activity_id' of the last item seen
):
    """
    Fetches a list of activities for the current user, sorted by date descending.
    Uses keyset (cursor-based) pagination for efficient loading.
    """
    q = select(model.ActivityTable).where(
        model.ActivityTable.owner_id == current_user_id.id)

    # Apply cursor conditions if provided (for subsequent pages)
    if cursor_date is not None and cursor_id is not None:
        # Fetch items older than the cursor date, or same date but smaller ID (since ID is random string, comparison works)
        # Note: Adjust comparison (< or >) based on desired sort order (DESC vs ASC)
        q = q.where(
            (model.ActivityTable.date < cursor_date) |
            ((model.ActivityTable.date == cursor_date) & (model.ActivityTable.activity_id < cursor_id))
        )

    # Always apply sorting and limit
    q = q.order_by(model.ActivityTable.date.desc(), model.ActivityTable.activity_id.desc()).limit(limit)

    results = session.exec(q).all()
    return results

@app_obj.patch("/activity/{activity_id}", response_model=model.ActivityBase)
async def update_activity(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    current_user_id: model.UserId = Depends(auth_handler.get_current_user_id),
    activity_id: str,
    activity_update: model.ActivityUpdate = Body(...)):

    q = select(model.ActivityTable).where(model.ActivityTable.activity_id == activity_id)
    activity_db = session.exec(q).one()
    if activity_db.owner_id != current_user_id.id:
        return HTTPException(status_code=401, detail="Not authorized: User doesn't own activity")
    activity_db.sqlmodel_update(activity_update.model_dump(exclude_unset=True))
    session.add(activity_db)
    session.commit()
    session.refresh(activity_db)
    return activity_db

@app_obj.delete("/activity/{activity_id}")
async def delete_activity(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    current_user_id: model.UserId = Depends(auth_handler.get_current_user_id),
    activity_id: str):
    """Deletes an activity owned by the current user."""
    activity_db = session.exec(
        select(model.ActivityTable).where(model.ActivityTable.activity_id == activity_id)
    ).first()

    if not activity_db:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")

    if activity_db.owner_id != current_user_id.id:
        # Use 403 Forbidden as the user is authenticated but not authorized for this resource
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized: User doesn't own activity")

    session.delete(activity_db)
    session.commit()

    # Return No Content response explicitly for DELETE success
    return Response(status_code=200)

@app_obj.post("/user/signup", tags=["user"])
async def create_user(
    *,
    session: Session = Depends(model_helpers.get_db_session),
    user: model.UserCreate = Body(...)):
    """
    Creates a new user account.

    This API endpoint allows users to register and create new accounts. The
    provided `user` data is validated against the `model.UserCreate` schema. 
    The password is hashed before saving it to the database for security 
    reasons.

    Args:
        session: A SQLAlchemy database session object (Obtained using Depends).
        user: An instance of `model.UserCreate` containing the new 
          user's information.

    Returns:
        A `model.Token` object containing the access token and token type
        upon successful registration.
    """
    db_user = model.User.model_validate(user)
    # Hash password before saving it
    db_user.password = crypto.get_password_hash(db_user.password)
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return model.Token(
        access_token=auth_handler.create_access_token(db_user),
        token_type="bearer")
