import { createContext, useContext } from 'react';
import { NoteRpcClient } from './notesRpc';

export const NotesRpcContext = createContext<NoteRpcClient | null>(null);

export function useNotesRpc(): NoteRpcClient {
  const client = useContext(NotesRpcContext);
  if (!client) throw new Error('NotesRpcContext is not provided');
  return client;
}
