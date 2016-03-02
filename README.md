App Engine application for the Udacity training course.

## Products
- [App Engine][1]

## Language
- [Python][2]

## APIs
- [Google Cloud Endpoints][3]

## Setup Instructions
1. Update the value of `application` in `app.yaml` to the app ID you
   have registered in the App Engine admin console and would like to use to host
   your instance of this sample.
1. Update the values at the top of `settings.py` to
   reflect the respective client IDs you have registered in the
   [Developer Console][4].
1. Update the value of CLIENT_ID in `static/js/app.js` to the Web client ID
1. (Optional) Mark the configuration files as unchanged as follows:
   `$ git update-index --assume-unchanged app.yaml settings.py static/js/app.js`
1. Run the app with the devserver using `dev_appserver.py DIR`, and ensure it's running by visiting your local server's address (by default [localhost:8080][5].)
1. (Optional) Generate your client library(ies) with [the endpoints tool][6].
1. Deploy your application.

## Implementation of sessions, speakers, and additional functionality (Task 1)

Sessions are implemented with a "has-a" relationship with conference.
In particular, a conference can have 0 or more sessions, and each
session must be associated with one conference. This follows the
intuition that a session wouldn't occur in multiple conferences and
any conference would likely have multiple sessions. This relationship
is specified by the conference attribute associated with each session,
which takes a KeyProperty(kind = Conference) data type.
Speakers are implemented as a property of sessions. In particular,
each session has a speaker. Duration of the session is a float property,
representing the number of hours.

There are also SessionForm and SessionForms objects which represent
protorpc message versions of the Session database objects suitable
for passing session information back and forth to the front end via
the session API.

##Two additional queries (Task 3)

One additional query (getConferenceSessionByDuration) checks to
return sessions falling within a particular duration. It also
accepts a conference key, so each query only returns sessions
within that particular conference.

The second query is getConferencesByCity which returns all of the
conferences available in a particular city.

## The problem with the provided query (Task 3)

The question asks about the problem with implementing a query for
non-workshops sessions before 7 PM. The problem with implementing
the query is that the datastore places a limitation on queries such
that you cannot use inequality filters on multiple properties. Here,
we are trying to do 2 inequality filters: a '!=' filter on Session type
and a '<=' on session time. Trying to do this will raise an exception.

## Solution to the problem with the provided query

My solution to this problem is to perform the two queries
separately, and then get the intersection of the two resulting
arrays of Session entities. I take the entity keys of the resulting
two session lists from the two queries, convert them to python Sets,
get the intersection, get the full session objects from the keys and
return the session forms. The solution is implemented in the
getSessionsNotEqualTypeBeforeTime endpoint method.


[1]: https://developers.google.com/appengine
[2]: http://python.org
[3]: https://developers.google.com/appengine/docs/python/endpoints/
[4]: https://console.developers.google.com/
[5]: https://localhost:8080/
[6]: https://developers.google.com/appengine/docs/python/endpoints/endpoints_tool
