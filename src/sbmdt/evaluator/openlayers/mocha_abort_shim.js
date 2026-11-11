/* SWE-bench harness: keep one late async error from cancelling the run.
 *
 * Mocha 9 treats an assertion that fires after its test has already
 * passed as an uncaught error. Runner#uncaught records the failure and
 * then calls Runner#abort, which drops every suite that has not run
 * yet. In openlayers-13020 a single such error in ol/View #animate()
 * ended the run at 393 of 2083 tests, in every patch type alike.
 *
 * The failure is recorded before abort is reached, so suppressing only
 * the abort keeps the grade honest -- the offending test still fails --
 * while letting the remaining suites run. OpenLayers does not pass
 * --bail, so nothing else in this configuration calls abort.
 */
(function () {
  function defuse() {
    var m = window.mocha;
    if (!m || typeof m.run !== 'function' || m.__abortDefused) {
      return false;
    }
    var run = m.run.bind(m);
    m.__abortDefused = true;
    m.run = function () {
      var runner = run.apply(null, arguments);
      if (runner && typeof runner.abort === 'function') {
        runner.abort = function () {
          return runner;
        };
      }
      return runner;
    };
    return true;
  }

  /* test-extensions.js is evaluated when the bundle loads, which is
   * normally before karma-mocha calls mocha.run(); if mocha is not on
   * window yet, catch it on the way into __karma__.start instead. */
  if (!defuse() && window.__karma__ && window.__karma__.start) {
    var start = window.__karma__.start;
    window.__karma__.start = function () {
      defuse();
      return start.apply(this, arguments);
    };
  }
})();
