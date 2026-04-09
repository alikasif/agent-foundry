from cl_agent import CommandLineAgent
from dotenv import load_dotenv


load_dotenv()


def main():
    agent = CommandLineAgent(model_prefix="GEMINI")
    print("Command Line Agent Initialized. Type 'exit' to quit.")

    while True:
        try:
            user_input = input("\nYou: ")
            if user_input.lower() in ["exit", "quit"]:
                break

            print("Generating command and executing...")
            result = agent.generate_command(user_input)
            print(f"Agent Output:\n{result}")

            feedback = input("\nFeedback (optional): ")
            if feedback:
                agent.learn(user_input, "See Agent Output", feedback)
                print("Feedback recorded.")

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"An error occurred: {e}")


if __name__ == "__main__":
    main()
