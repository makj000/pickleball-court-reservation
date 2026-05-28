const SECRET_PROPERTY = 'PICKLEBALL_CALENDAR_SECRET';

function doPost(e) {
  try {
    const body = JSON.parse((e.postData && e.postData.contents) || '{}');
    const secret = PropertiesService.getScriptProperties().getProperty(SECRET_PROPERTY);
    if (!secret || body.secret !== secret) {
      return jsonResponse({ok: false, error: 'Unauthorized'});
    }

    const calendar = CalendarApp.getDefaultCalendar();
    const start = new Date(body.start_iso);
    const end = new Date(body.end_iso);
    const sourceId = String(body.source_id || '');
    const existing = sourceId
      ? calendar.getEvents(start, end, {search: sourceId})
      : [];
    if (existing.length > 0) {
      return jsonResponse({ok: true, duplicate: true, event_id: existing[0].getId()});
    }

    const attendees = Array.isArray(body.attendees) ? body.attendees.join(',') : '';
    const event = calendar.createEvent(
      body.summary || `Pickleball Court ${body.court}`,
      start,
      end,
      {
        location: body.location || 'Foster City Pickleball Courts',
        description: body.description || '',
        guests: attendees,
        sendInvites: true
      }
    );
    event.setGuestsCanInviteOthers(false);

    return jsonResponse({ok: true, event_id: event.getId()});
  } catch (err) {
    return jsonResponse({ok: false, error: String(err)});
  }
}

function jsonResponse(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
