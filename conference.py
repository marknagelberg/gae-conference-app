#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
from sets import Set
import logging

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import ConferenceCityQueryForm
from models import TeeShirtSize
from models import SessionForm
from models import SessionForms
from models import Session
from models import SessionTypeFilterForm
from models import SessionSpeakerFilterForm
from models import SessionDurationFilterForm
from models import SessionWishList
from models import SessionWishListForm
from models import ChangeWishList
from models import SessionNotTypeBeforeTimeForm

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        #if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        #else:
                        #    setattr(prof, field, val)
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")




# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(ConferenceCityQueryForm, ConferenceForms,
            path='session/filterbyconferencecity',
            http_method='GET', name='getConferencesByCity')
    def getConferencesByCity(self, request):
        """Return all conferences in given city."""

        q = Conference.query(request.city == Conference.city)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )


@endpoints.api(name='session', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID], scopes=[EMAIL_SCOPE])
class SessionApi(remote.Service):
    """Session API v0.1"""

# - - - Session objects - - - - - - - - - - - - - - - - -

    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm."""
        sesh = SessionForm()
        for field in sesh.all_fields():
            if hasattr(session, field.name):
                #Convert dates and times to strings, copy others
                if field.name.endswith('date') or field.name.endswith('start_time'):
                    setattr(sesh, field.name, str(getattr(session, field.name)))
                else:
                    setattr(sesh, field.name, getattr(session, field.name))
            elif field.name == "websafeConferenceKey":
                setattr(sesh, field.name, session.conference.urlsafe())
        sesh.check_initialized()
        return sesh


    def _createSession(self, request):
        """Create or update Session object, returning SessionForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # get conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        #Check to make sure user is conference owner.
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException('Only the conference creator can add sessions')

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields() if field.name != 'websafeConferenceKey'}

        data['conference'] = conf.key

        # convert dates from strings to Date objects; set month based on start_date
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
        # Convert start time from strings to time object (24 hour clock)
        if data['start_time']:
            data['start_time'] = datetime.strptime(data['start_time'][:5], "%H:%M").time()

        # create Session
        Session(**data).put()

        taskqueue.add(params={'speaker': request.speaker,
                              'websafeConferenceKey': request.websafeConferenceKey},
            url='/tasks/update_featured_speaker'
        )

        return request


    @endpoints.method(SessionForm, SessionForm,
            path='session',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session (only conference organizer can
           do this)."""
        return self._createSession(request)


    @endpoints.method(CONF_GET_REQUEST, SessionForms,
            path='session/{websafeConferenceKey}',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Return requested sessions for a conference (by websafeConferenceKey)."""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        q = Session.query()
        q = q.filter(Session.conference==conf.key)

        return SessionForms(
            items=[self._copySessionToForm(sesh) for sesh in q]
        )


    @endpoints.method(SessionTypeFilterForm, SessionForms,
            path='session/filterbysessiontype',
            http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Return requested sessions with given type for a conference."""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        q = Session.query()

        q = q.filter(Session.conference==conf.key, Session.typeOfSession == request.typeOfSession)
        return SessionForms(
            items=[self._copySessionToForm(sesh) for sesh in q]
        )

    @endpoints.method(SessionNotTypeBeforeTimeForm, SessionForms,
            path='session/filterbysessionnottpebeforetime',
            http_method='GET', name='getSessionsNotEqualTypeBeforeTime')
    def getSessionsNotEqualTypeBeforeTime(self, request):
        """Return requested sessions with given type for a conference."""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        not_equals_type_query = Session.query()
        not_equals_type_query = not_equals_type_query.filter(Session.conference==conf.key, Session.typeOfSession != request.notType)

        not_equals_sessions = Set([sesh.key for sesh in not_equals_type_query])

        before_time_query = Session.query()
        before_time_query = before_time_query.filter(Session.conference==conf.key, Session.start_time <= request.beforeTime)
        before_time_sessions = Set([sesh.key for sesh in before_time_query])

        sessions_to_return = [x.get() for x in before_time_sessions & not_equals_sessions]

        return SessionForms(
            items=[self._copySessionToForm(sesh) for sesh in sessions_to_return]
        )



    @endpoints.method(SessionSpeakerFilterForm, SessionForms,
            path='session/filterbysessionspeaker',
            http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Return requested sessions with given speaker, across all conferences."""

        if not request.speaker:
            raise endpoints.BadRequestException("You must provide a speaker!")
        q = Session.query(request.speaker == Session.speaker)

        return SessionForms(
            items=[self._copySessionToForm(sesh) for sesh in q]
        )


    @endpoints.method(SessionDurationFilterForm, SessionForms,
            path='session/filterbysessionduration',
            http_method='GET', name='getConferenceSessionsByDuration')
    def getConferenceSessionsByDuration(self, request):
        """Return requested sessions with given duration range for a conference."""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        q = Session.query()

        if not request.min and not request.max:
            raise endpoints.BadRequestException("Must include a min duration or a max duration")
        elif not request.min:
            q = q.filter(Session.conference==conf.key, Session.duration <= request.max)
        elif not request.max:
            q = q.filter(Session.conference==conf.key, Session.duration >= request.min)
        else:
            q = q.filter(Session.conference==conf.key, Session.duration <= request.max, Session.duration >= request.min)
        return SessionForms(
            items=[self._copySessionToForm(sesh) for sesh in q if sesh.duration]
        )

# - - - WishList objects - - - - - - - - - - - - - - - - -

    def _copySessionWishListToForm(self, session_wishlist):
        """Copy relevant fields from Conference to ConferenceForm."""
        sesh = SessionWishListForm()
        sesh.userID = session_wishlist.userID
        sesh.wishlistSessions = [self._copySessionToForm(session.get()) for session in session_wishlist.wishlistSessions]
        #what does line below do?
        sesh.check_initialized()
        return sesh

    @endpoints.method(ChangeWishList, SessionForm,
            path='addSessionToWishList',
            http_method='POST', name='addSessionToWishList')
    def addSessionToWishList(self, request):
        """Add a new session to the user's wish list."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        #Need to check to make sure session key refers to actual session,
        #also want to check that the user is registered for the relevant
        #conference.
        sesh = ndb.Key(urlsafe = request.sessionKey)
        sesh_key = sesh.get()


        if not sesh:
            raise endpoints.NotFoundException('Session with that key does not exist!')
        conf_for_sesh_key = sesh_key.conference
        conf_for_sesh = conf_for_sesh_key.get()

        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))

        if conf_for_sesh not in confs:
            endpoints.UnauthorizedException('You are not attending the conference with that session!')

        sesh_query = ndb.Key(SessionWishList, user_id).get()
        if not sesh_query:
            #Make UserID the key
            new_sesh = SessionWishList(userID = user_id, wishlistSessions = [sesh])
            new_sesh.key = ndb.Key(SessionWishList, new_sesh.userID)
            new_sesh.put()
            return self._copySessionToForm(sesh_key)
        if sesh in sesh_query.wishlistSessions:
            raise endpoints.UnauthorizedException('That session is already in your wishlist!')
        sesh_query.wishlistSessions.append(sesh)
        sesh_query.put()
        return self._copySessionToForm(sesh_key)


    @endpoints.method(message_types.VoidMessage, SessionWishListForm,
            path='getSessionsInWishlist',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Query for all sessions the user is interested in."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        sesh_query = ndb.Key(SessionWishList, user_id).get()
        if not sesh_query:
            endpoints.NotFoundException('You do not current have a wishlist!')
        return self._copySessionWishListToForm(sesh_query)


    @endpoints.method(ChangeWishList, SessionForm,
            path='deleteSessionFromWishList',
            http_method='POST', name='deleteSessionFromWishList')
    def deleteSessionFromWishList(self, request):
        """Delete session from the user's wish list."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        #Need to check to make sure session key refers to actual session,
        #also want to check that the user is registered for the relevant
        #conference.
        sesh = ndb.Key(urlsafe = request.sessionKey)
        sesh_key = sesh.get()

        if not sesh:
            raise endpoints.NotFoundException('Session with that key does not exist!')

        sesh_query = ndb.Key(SessionWishList, user_id).get()
        if sesh not in sesh_query.wishlistSessions:
            raise endpoints.NotFoundException('Session does not exist in wishlist!')
        sesh_query.wishlistSessions.remove(sesh)
        sesh_query.put()
        return self._copySessionToForm(sesh_key)


# - - - Featured speaker - - - - - - - - - - - - - - - - - - - -

    @endpoints.method(StringMessage, StringMessage,
            path='session/featured_speaker/get',
            http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return featured speaker from memcache using conference key."""
        return StringMessage(data=memcache.get(request.data) or "")


api = endpoints.api_server([ConferenceApi, SessionApi]) # register API
