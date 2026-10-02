# Player Koi — explainer narration

Audience: general / YouTube viewers. Spoken by Edge TTS (`en-US-AndrewNeural`).
This is the source of truth for the voiceover text in `scenes.py`. Keep the two in sync.

## 1. Hook
Chess is more popular than ever, but almost all of it happens on a screen.
A real board needs a second player, so when you're alone, it stays in the box.
So we asked: what if the board itself could play back? Meet Player Koi.

## 2. Core idea
Player Koi is a real chessboard with three new abilities.
Eyes: a camera above the board watches every square.
A brain: a Raspberry Pi 5 that knows the rules and runs the Stockfish chess engine.
And a hand: a magnet hidden under the board that slides the computer's pieces.
You move a piece by hand. It sees your move, thinks, and plays its reply.

## 3. How it sees
How does it see your move? First, a simple motion detector waits for your hand
to enter the board, and then leave. Then a small AI model looks at all sixty-four
squares, and answers just one question for each: empty, white, or black.
It never needs to know which piece is which. The game starts from the standard
position, so the software already knows. Comparing before and after, two squares
changed: e2 became empty, and e4 turned white. That change is checked against
every legal move, and exactly one fits: pawn to e4. And if the picture is unclear,
or more than one move fits, it doesn't guess. It flags the squares and asks you.

## 4. How it moves
Now it's the computer's turn. Stockfish picks a reply. The Pi sends the move over
USB to an Arduino Uno, which drives two stepper motors on a CoreXY frame under
the board, and switches an electromagnet on. The magnet grabs the piece through
the board and slides it to its square. Knights are trickier, because they can't
jump. So the magnet steers them along the gaps between the squares, past the other
pieces. For a capture, the arm first carries your piece off to a parking ring
around the board, then moves its own piece in.

## 5. Coach and safety
Player Koi can also teach. In guided mode, a coach draws the best move as a green
arrow and explains why. After you move, it grades your move, from best all the
way down to blunder. There are thousands of puzzles too, picked near your rating.
And it checks its own work: after every robot move, the camera reads all
sixty-four squares again. If a piece slipped, or a hand got in the way, the arm
stops, and you fix it with undo or edit board.

## 6. Outro
Player Koi. A real chessboard that sees, thinks, and moves.
