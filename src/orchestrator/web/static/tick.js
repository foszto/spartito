'use strict';
// A dedicated worker that ticks for the page (app.js: the notice poll). A hidden page's own
// timers are throttled (Chrome: once a minute after five minutes hidden); a worker's are not
// tied to the page's visibility. The page sends the interval in ms; each tick posts a message.
let timer = null;
onmessage = event => {
  clearInterval(timer);
  timer = setInterval(() => postMessage(null), Number(event.data) || 4000);
};
