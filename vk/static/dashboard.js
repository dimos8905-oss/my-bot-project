function initDashboardSocket() {
    const socket = io();
    socket.on("new_request", data => {
        const div = document.createElement("div");
        div.className = "card";
        div.onclick = () => window.location.href = '/chat/' + data.user_id;
        div.innerHTML = `<div><b>${data.username}</b><br><small>${data.question}</small></div>
                         <div><span class='status Открыто'>Открыто</span></div>`;
        document.getElementById("cards").prepend(div);
    });
}
