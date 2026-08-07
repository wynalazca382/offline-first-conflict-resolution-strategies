import { invoke } from "@tauri-apps/api/core";

interface Note {
  id: string;
  title: string;
  content: string;
  created_at: number;
  updated_at: number;
  is_deleted: boolean;
  vector_clock: string | null;
}

let editingId: string | null = null;

async function loadNotes() {
  const notes: Note[] = await invoke("get_notes");
  const list = document.getElementById("notes-list")!;
  list.innerHTML = "";
  notes.forEach(note => {
    const div = document.createElement("div");
    div.className = "note";
    div.innerHTML = `
      <h3>${note.title}</h3>
      <p>${note.content}</p>
      <small>${new Date(note.updated_at).toLocaleString()}</small>
      <button onclick="editNote('${note.id}', '${note.title.replace(/'/g, "\\'")}', '${note.content.replace(/'/g, "\\'")}')">Edytuj</button>
      <button onclick="deleteNote('${note.id}')">Usuń</button>
    `;
    list.appendChild(div);
  });
}

async function saveNote() {
  const title = (document.getElementById("title") as HTMLInputElement).value;
  const content = (document.getElementById("content") as HTMLTextAreaElement).value;
  if (!title.trim()) return;

  if (editingId) {
    await invoke("update_note", { id: editingId, title, content });
    editingId = null;
    document.getElementById("save-btn")!.textContent = "Dodaj notatkę";
  } else {
    await invoke("create_note", { title, content });
  }

  (document.getElementById("title") as HTMLInputElement).value = "";
  (document.getElementById("content") as HTMLTextAreaElement).value = "";
  await loadNotes();
}

(window as any).editNote = (id: string, title: string, content: string) => {
  editingId = id;
  (document.getElementById("title") as HTMLInputElement).value = title;
  (document.getElementById("content") as HTMLTextAreaElement).value = content;
  document.getElementById("save-btn")!.textContent = "Zapisz zmiany";
};

(window as any).deleteNote = async (id: string) => {
  await invoke("delete_note", { id });
  await loadNotes();
};

document.getElementById("save-btn")!.addEventListener("click", saveNote);
loadNotes();