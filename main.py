# !/usr/bin/env python

"""
main.py -- Udacity conference server-side Python App Engine
    HTTP controller handlers for memcache & task queue access

$Id$

created by wesc on 2014 may 24

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'

import webapp2
from google.appengine.api import app_identity
from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.ext import ndb
from conference import ConferenceApi
import logging
from models import Session

FEATURED_SPEAKER_MESSAGE = "Featured Speaker %s is conducting the following sessions: %s"

class SetAnnouncementHandler(webapp2.RequestHandler):
    def get(self):
        """Set Announcement in Memcache."""
        ConferenceApi._cacheAnnouncement()
        self.response.set_status(204)


class SendConfirmationEmailHandler(webapp2.RequestHandler):
    def post(self):
        """Send email confirming Conference creation."""
        mail.send_mail(
            'noreply@%s.appspotmail.com' % (
                app_identity.get_application_id()),     # from
            self.request.get('email'),                  # to
            'You created a new Conference!',            # subj
            'Hi, you have created a following '         # body
            'conference:\r\n\r\n%s' % self.request.get(
                'conferenceInfo')
        )

class SendFeaturedSpeakerUpdate(webapp2.RequestHandler):
    def post(self):
        """Check new session's speaker should be featured. Featured
           speakers are those that appear in more than one session
           for a given conference."""
        speaker = self.request.get('speaker')
        logging.debug("speaker: " + speaker)
        websafeConferenceKey = self.request.get('websafeConferenceKey')

        q = Session.query(Session.speaker == speaker,
                ndb.Key(urlsafe = websafeConferenceKey) == Session.conference)
        if len(q.fetch()) > 1:
            featured_key = websafeConferenceKey
            # add new memcache entry that features speaker and session names
            featured_speaker = FEATURED_SPEAKER_MESSAGE % (speaker, ','.join([sesh.name for sesh in q]))
            memcache.set(featured_key, featured_speaker)


app = webapp2.WSGIApplication([
    ('/crons/set_announcement', SetAnnouncementHandler),
    ('/tasks/send_confirmation_email', SendConfirmationEmailHandler),
    ('/tasks/update_featured_speaker', SendFeaturedSpeakerUpdate)
], debug=True)
