import { useEffect, useRef } from 'react';
import { ChatMessage } from './ChatMessage';
import { useApp } from '../../store/AppContext';

export function ChatArea({ onRun, onWizardOpen }) {
  const { chatHistory } = useApp();
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatHistory]);

  if (chatHistory.length === 0) {
    return <div className="flex-1" />;
  }

  return (
    <div className="flex-1 overflow-y-auto py-4 space-y-1">
      {chatHistory.map(msg => (
        <ChatMessage
          key={msg.id}
          message={msg}
          onRun={onRun}
          onWizardOpen={onWizardOpen}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
