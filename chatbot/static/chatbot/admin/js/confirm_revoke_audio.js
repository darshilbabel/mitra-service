// Confirms before submitting a "Revoke Audio" inline action button.
// The inline_actions package renders plain <input type="submit"> with no
// hook for onclick, so we intercept clicks on the css class it lets us set
// via CompanyStateMachineAdmin.get_revoke_audio_css.
document.addEventListener('click', function (event) {
    var target = event.target;
    if (target && target.matches && target.matches('input.confirm-revoke-audio')) {
        if (!window.confirm('Revoke cached audio for this step? This deletes the audio file(s) from S3 and cannot be undone.')) {
            event.preventDefault();
        }
    }
});
